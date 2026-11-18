FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Pythonおよびuvの動作設定
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# ボリュームマウント時に仮想環境が上書きされないよう /venv に配置
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV PATH="/venv/bin:$PATH"

# 依存関係定義のコピーとインストール (キャッシュ活用)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# アプリケーションコードのコピー
COPY . .

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8181

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8181"]
