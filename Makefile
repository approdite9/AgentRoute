# 常用运维入口。约定：宿主侧命令在 `conda activate Agent` 环境下执行。
#
# 多进程指标目录：dev / worker 都导出同一 PROMETHEUS_MULTIPROC_DIR，
# uvicorn 与 celery worker 自增的指标才能被 /metrics 聚合（详见 monitoring/metrics.py）。

PROM_DIR := $(PWD)/monitoring/prom_multiproc

.PHONY: dev worker flower test migrate docker-up docker-down

# 本地开发：起 redis/postgres 容器，后台跑 uvicorn（--reload），前台跑 streamlit。
dev:
	docker compose up -d redis postgres
	mkdir -p $(PROM_DIR)
	PROMETHEUS_MULTIPROC_DIR=$(PROM_DIR) uvicorn api.main:app --reload --port 8000 & \
	PROMETHEUS_MULTIPROC_DIR=$(PROM_DIR) streamlit run app.py

# Celery worker：消费 3 个队列，并发 3。共享同一指标目录。
worker:
	mkdir -p $(PROM_DIR)
	PROMETHEUS_MULTIPROC_DIR=$(PROM_DIR) celery -A tasks.celery_app worker \
		-Q trip.planning,trip.weather,trip.poi --concurrency=3 --loglevel=info -E

# Celery 任务监控面板（http://localhost:5555）。
flower:
	celery -A tasks.celery_app flower

# 测试 + 覆盖率。
test:
	pytest tests/ -v --cov=.

# 应用数据库迁移到最新。
migrate:
	alembic upgrade head

# 全栈：构建并后台启动所有服务。
docker-up:
	docker compose up -d --build

# 停止并移除全栈容器（保留命名卷数据）。
docker-down:
	docker compose down
