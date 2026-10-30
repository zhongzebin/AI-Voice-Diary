"""数据模型定义"""
from typing import List, Optional
from pydantic import BaseModel, Field


class DiaryEntryMetadata(BaseModel):
    """一条日记记录(原始识别文本不落盘)"""
    id: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    summary: str
    tags: List[str] = Field(default_factory=list)
    entities: dict = Field(default_factory=dict)  # {people, places, events, emotion}


class EntryUpdate(BaseModel):
    summary: str = Field(min_length=1, max_length=5000)
    tags: List[str] = Field(default_factory=list, max_length=10)


class QARequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: List[dict] = Field(default_factory=list)


class ConfigSetup(BaseModel):
    """保存配置; key 留空表示沿用已保存的值"""
    data_path: str = Field(min_length=1)
    dashscope_key: Optional[str] = None
    deepseek_key: Optional[str] = None


class ConfigTest(BaseModel):
    """测试连通性; key 留空则测试已保存的值"""
    dashscope_key: Optional[str] = None
    deepseek_key: Optional[str] = None
