# ══════════════════════════════════════════════════════════════
# deploy/Dockerfile · 免卡 PaaS 部署（Koyeb/Render/Fly/Railway 通用）
# ──────────────────────────────────────────────────────────────
# 把自包含交易器打成容器，丢到免费 PaaS 上 24×7 跑（无需信用卡·无需 SSH）。
# 只跑确定性策略核·零 LLM·纸面·真钱永不自动。
# 本地测：docker build -t vps-trader deploy && docker run vps-trader
# ══════════════════════════════════════════════════════════════
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir numpy pandas requests
COPY vps_trader.py /app/

# PaaS 上账户/日志建议挂持久卷到 /app（免费档多为临时盘·重启清空属预期）
ENV PYTHONUNBUFFERED=1
CMD ["python", "vps_trader.py"]
