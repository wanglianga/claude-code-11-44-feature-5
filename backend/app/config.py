"""应用配置：全部可通过环境变量覆盖，本地默认值对应容器内预装 PostgreSQL。"""
import os


class Settings:
    # 本地开发默认连容器自带 PostgreSQL；Compose 中由环境变量覆盖为 db 服务名
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/coldchain",
    )
    # 消费端分页大小（模拟消费端按页拉取）
    CONSUMER_PAGE_SIZE: int = int(os.getenv("CONSUMER_PAGE_SIZE", "5"))
    # 标准温度的物理合理区间（校验用，超出标记 out_of_range，不丢弃、不静默通过）
    VALID_TEMP_MIN_C: float = float(os.getenv("VALID_TEMP_MIN_C", "-60"))
    VALID_TEMP_MAX_C: float = float(os.getenv("VALID_TEMP_MAX_C", "100"))


settings = Settings()

# 质量发布用途门槛：只有达到相应用途要求的数据才进入该用途的发布版本
PURPOSES = {
    # 线路监控：需要可信采样时间，时间误差不超过 15 分钟，归属可选
    "line_monitor": {"max_time_error_s": 900.0, "require_attribution": False},
    # 批次放行审计：需要完整归属（车辆/周转箱/货物批次）且时间误差不超过 5 分钟
    "batch_audit": {"max_time_error_s": 300.0, "require_attribution": True},
}
