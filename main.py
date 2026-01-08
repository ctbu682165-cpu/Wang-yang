import asyncio
import aiohttp
import csv
from datetime import datetime, timedelta
from typing import Generator, Optional

from fastapi.templating import Jinja2Templates
#  from fastapi.responses import TemplateResponse  # 从responses模块导入
from fastapi import FastAPI, Request, Form, Depends, HTTPException, Path
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# 导入数据模型（解耦模型定义与业务逻辑）
from models import Base, City, DefaultCity

# -------------------------- 1. 基础配置（符合可维护性要求） --------------------------
# 数据库配置（SQLite，避免多线程问题）
DATABASE_URL = "sqlite:///./cities.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite单线程限制解决方案
    echo=False  # 生产环境关闭SQL日志，减少性能消耗
)
# 数据库会话工厂（每次请求独立会话）
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# FastAPI应用初始化
app = FastAPI(
    title="Python Weather Web App",
    description="2025秋季硕士实验任务:基于FastAPI的城市天气查询应用",
    version="1.0.0"
)
# 模板引擎配置（指定前端页面目录）
templates = Jinja2Templates(directory="templates")

# -------------------------- 2. 数据库依赖（统一会话管理） --------------------------
def get_db() -> Generator[Session, None, None]:
    """数据库会话依赖项：每个请求创建独立会话，请求结束自动关闭
    
    避免连接泄漏，确保事务一致性，符合最佳实践
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------------------------- 3. 核心工具函数（单一职责原则） --------------------------
async def fetch_weather(latitude: float, longitude: float) -> float:
    """异步请求Open-Meteo API获取实时温度(摄氏度)
    
    参数：
        latitude: 城市纬度(范围：-90 ~ 90)
        longitude: 城市经度(范围：-180 ~ 180)
    
    返回：
        float: 实时温度值
    
    异常：
        HTTPException: API请求失败或数据解析错误时抛出
    """
    # API请求地址（严格遵循Open-Meteo接口规范）
    api_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        f"&current_weather=true&hourly=temperature_2m"
    )
    
    try:
        # 异步请求（aiohttp，符合实验异步要求）
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=503,
                        detail=f"天气API请求失败，状态码：{response.status}"
                    )
                # 解析API响应数据
                response_data = await response.json()
                return response_data["current_weather"]["temperature"]
    
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=500, detail=f"网络请求错误：{str(e)}")
    except KeyError as e:
        raise HTTPException(status_code=500, detail=f"API响应数据格式错误：缺少{e}字段")


def init_default_cities(db: Session) -> None:
    """从europe.csv初始化默认城市表（仅首次启动执行，避免重复导入）
    
    参数：
        db: 数据库会话对象
    """
    # 检查默认表是否已初始化，避免重复操作
    if db.query(DefaultCity).first() is not None:
        return
    
    # 读取CSV文件（处理编码与文件不存在异常）
    try:
        with open("europe.csv", "r", encoding="utf-8-sig") as csv_file:
            # 按CSV表头解析数据（匹配europe.csv格式）
            csv_reader = csv.DictReader(csv_file)
            for row in csv_reader:
                # 数据类型转换与校验（避免无效数据）
                try:
                    latitude = float(row["latitude"])
                    longitude = float(row["longitude"])
                    # 纬度/经度范围校验（符合地理常识）
                    if not (-90 <= latitude <= 90):
                        raise ValueError(f"纬度{latitude}超出范围（-90~90）")
                    if not (-180 <= longitude <= 180):
                        raise ValueError(f"经度{longitude}超出范围（-180~180）")
                    
                    # 添加默认城市到数据库
                    default_city = DefaultCity(
                        name=row["name"].strip(),
                        latitude=latitude,
                        longitude=longitude
                    )
                    db.add(default_city)
                
                except ValueError as e:
                    # 跳过无效数据，不中断整体初始化
                    print(f"跳过无效城市数据：{row['name']}，原因：{str(e)}")
        
        # 提交事务（确保数据写入）
        db.commit()
        print("默认城市表初始化完成（数据来源：europe.csv）")
    
    except FileNotFoundError:
        raise RuntimeError("初始化失败：未找到europe.csv文件，请检查文件路径")
    except csv.Error as e:
        raise RuntimeError(f"CSV文件解析错误：{str(e)}")


def reset_cities_to_default(db: Session) -> None:
    """重置城市列表：清空当前City表，从DefaultCity同步默认数据
    
    参数：
        db: 数据库会话对象
    """
    # 清空当前城市表（先删后加，避免重复）
    db.query(City).delete()
    # 从默认表查询所有城市
    default_cities = db.query(DefaultCity).all()
    if not default_cities:
        raise HTTPException(status_code=500, detail="默认城市数据为空，无法重置")
    
    # 同步默认城市到当前表
    for default_city in default_cities:
        db.add(City(
            name=default_city.name,
            latitude=default_city.latitude,
            longitude=default_city.longitude,
            temperature=None,  # 初始无温度数据
            updated_at=None    # 初始无更新时间
        ))
    
    db.commit()


def check_update_cooldown(db: Session) -> bool:
    """检查温度更新冷却时间（15分钟内不允许重复更新）
    
    参数：
        db: 数据库会话对象
    
    返回：
        bool: True=可更新（超过15分钟），False=不可更新（冷却中）
    """
    # 查询最后一次更新时间（取所有城市中最新的updated_at）
    last_updated = db.query(City.updated_at).order_by(City.updated_at.desc()).first()
    if not last_updated or last_updated[0] is None:
        # 无更新记录，允许更新
        return True
    
    # 计算当前时间与最后更新时间的差值
    time_diff = datetime.now() - last_updated[0]
    # 15分钟冷却判断（转换为秒：15*60=900秒）
    return time_diff.total_seconds() >= 900

# -------------------------- 4. 启动事件（初始化操作） --------------------------
@app.on_event("startup")
def startup_init() -> None:
    """应用启动时执行的初始化操作：
    1. 创建所有数据库表（若不存在）
    2. 初始化默认城市表（从europe.csv导入）
    3. 若当前城市表为空，自动同步默认数据
    """
    # 创建数据库表（基于models定义）
    Base.metadata.create_all(bind=engine)
    # 获取数据库会话
    db = next(get_db())
    try:
        # 初始化默认城市表
        init_default_cities(db)
        # 同步默认数据到当前城市表（若为空）
        if db.query(City).count() == 0:
            reset_cities_to_default(db)
            print("当前城市表为空，已自动同步默认城市数据")
    finally:
        db.close()

# -------------------------- 5. 路由接口（完全匹配实验要求） --------------------------
@app.get("/", response_class=HTMLResponse, summary="首页：展示城市天气列表")
async def read_index(
    request: Request,
    db: Session = Depends(get_db)
):  # 移除-> TemplateResponse
    cities = db.query(City).order_by(
        City.temperature.desc().nullslast()
    ).all()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "cities": cities}
    )


@app.post("/cities/add", summary="添加新城市")
async def add_city(
    name: str = Form(..., min_length=1, max_length=50, description="城市名称（不可重复）"),
    latitude: float = Form(..., ge=-90, le=90, description="纬度（-90~90）"),
    longitude: float = Form(..., ge=-180, le=180, description="经度（-180~180）"),
    db: Session = Depends(get_db)
) -> RedirectResponse:
    """添加新城市到列表，包含数据校验（名称唯一、经纬度范围）"""
    # 检查城市名称是否已存在（避免重复）
    if db.query(City).filter(City.name == name.strip()).first():
        raise HTTPException(status_code=400, detail=f"城市「{name}」已存在，不可重复添加")
    
    # 创建新城市记录
    new_city = City(
        name=name.strip(),
        latitude=latitude,
        longitude=longitude
    )
    db.add(new_city)
    db.commit()
    
    # 重定向到首页（避免表单重复提交）
    return RedirectResponse(url="/", status_code=303)


@app.post("/cities/remove/{city_id}", summary="删除指定城市")
async def remove_city(
    city_id: int = Path(..., ge=1, description="城市ID（正整数）"),
    db: Session = Depends(get_db)
) -> RedirectResponse:
    """根据城市ID删除记录，若ID不存在则抛出404异常"""
    # 查询要删除的城市
    city = db.query(City).filter(City.id == city_id).first()
    if not city:
        raise HTTPException(status_code=404, detail=f"城市ID {city_id} 不存在，无法删除")
    
    # 执行删除
    db.delete(city)
    db.commit()
    
    # 重定向到首页
    return RedirectResponse(url="/", status_code=303)


@app.post("/cities/reset", summary="重置城市列表")
async def reset_cities(
    db: Session = Depends(get_db)
) -> RedirectResponse:
    """将当前城市列表重置为默认数据（从DefaultCity同步）"""
    reset_cities_to_default(db)
    return RedirectResponse(url="/", status_code=303)


@app.post("/cities/update", summary="更新所有城市温度")
async def update_weather(
    db: Session = Depends(get_db)
) -> RedirectResponse:
    """异步更新所有城市温度，包含15分钟冷却限制，避免频繁请求API"""
    # 检查冷却时间（符合实验“15分钟不重复更新”要求）
    if not check_update_cooldown(db):
        raise HTTPException(
            status_code=400,
            detail="距离上次更新不足15分钟,请稍后再试"
        )
    
    # 查询所有需要更新的城市
    cities = db.query(City).all()
    if not cities:
        raise HTTPException(status_code=400, detail="当前无城市数据，无法更新温度")
    
    # 异步批量获取所有城市温度（asyncio.gather，符合实验异步要求）
    # 创建任务列表：每个城市对应一个天气请求任务
    tasks = [fetch_weather(city.latitude, city.longitude) for city in cities]
    try:
        # 并发执行所有异步任务
        temperatures = await asyncio.gather(*tasks)
    except HTTPException as e:
        # 捕获API请求异常，返回详细错误信息
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    
    # 更新数据库中的温度和时间
    current_time = datetime.now()
    for city, temp in zip(cities, temperatures):
        city.temperature = temp
        city.updated_at = current_time
    
    db.commit()
    return RedirectResponse(url="/", status_code=303)