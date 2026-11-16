FROM node:20-bookworm-slim AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build:css && test -s node_modules/axe-core/axe.min.js

FROM python:3.11-bookworm AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium
COPY backend ./backend
COPY tests/fixtures/layout.html ./tests/fixtures/layout.html
COPY --from=assets /build/frontend ./frontend
COPY --from=assets /build/node_modules/axe-core/axe.min.js ./node_modules/axe-core/axe.min.js
COPY --from=assets /build/node_modules/axe-core/LICENSE ./node_modules/axe-core/LICENSE
RUN python -c "from backend.engine.a11y_scanner import AXE_PATH; assert AXE_PATH.is_file() and AXE_PATH.stat().st_size > 0, 'axe-core runtime asset missing'"
ENV PUBLIC_DEMO_MODE=true
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]