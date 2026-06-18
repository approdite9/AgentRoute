# ----------------------------------------------------------------------------
# 多阶段构建：builder 预编译依赖 wheel，runtime 只装 wheel + 拷源码，非 root 运行。
#
# 说明（与 Sprint 10 原始模板的差异）：
#   原模板 builder 用 `pip wheel . `（构建项目本身的 wheel）。本项目是 flat-layout
#   且 pyproject 的 build-backend 指向不存在的 `setuptools.backends.legacy`，直接
#   `pip wheel .` 会因「多顶层包自动发现」失败。应用本身以源码方式运行
#   （uvicorn api.main:app / celery -A tasks.celery_app / streamlit run app.py），
#   无需把项目打成 wheel。故 builder 仅为「依赖项」预构建 wheel，runtime 再装上，
#   既保留多阶段的体积/缓存优势，又不依赖项目可打包。
# ----------------------------------------------------------------------------

FROM python:3.13-slim AS builder
WORKDIR /build
COPY pyproject.toml .
# 从 pyproject 抽出 [project].dependencies → requirements.txt，再预构建所有依赖 wheel。
RUN pip install --no-cache-dir build && \
    python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); open('requirements.txt','w').write(chr(10).join(d['project']['dependencies']))" && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.13-slim AS runtime
# 非 root 运行用户（uid 固定 1000，便于宿主卷权限对齐）。
RUN useradd -m -u 1000 appuser
WORKDIR /app

# 装入预构建的依赖 wheel 后即删除，保持 runtime 镜像精简。
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# 拷入应用源码（worker / flower 容器不挂载源码卷，依赖镜像内这份）。
COPY . .

# Prometheus 多进程指标目录：api 与 worker 通过同名卷挂载到此同一路径，
# api 的 /metrics 才能聚合 worker 进程写入的业务指标。预先建好并归属 appuser，
# 空命名卷首次挂载会继承镜像内该目录的属主，从而让非 root 的 appuser 可写。
RUN mkdir -p /prom_multiproc && chown appuser:appuser /prom_multiproc

# flat-layout：把 /app 钉到导入路径，保证 api.* / tasks.* / agents.* / app 可被导入。
ENV PYTHONPATH=/app

USER appuser
EXPOSE 8000 8501
