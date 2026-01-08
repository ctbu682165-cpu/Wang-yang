from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base

# 基础模型类，所有数据表模型继承此类
Base = declarative_base()


class City(Base):
    """城市数据表模型（存储当前展示的城市及天气数据）
    
    字段说明：
    - id: 城市唯一标识（自增主键）
    - name: 城市名称（唯一，不可重复）
    - latitude: 城市纬度（范围：-90 ~ 90）
    - longitude: 城市经度（范围：-180 ~ 180）
    - temperature: 实时温度（可空，初始无数据）
    - updated_at: 最后更新时间（可空，记录温度更新时间）
    """
    __tablename__ = "cities"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(50), unique=True, index=True, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    temperature = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class DefaultCity(Base):
    """默认城市数据表模型（存储csv初始化的原始城市，用于重置功能）
    
    字段说明与City一致，无温度和更新时间字段（仅存储基础地理信息）
    """
    __tablename__ = "default_cities"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)