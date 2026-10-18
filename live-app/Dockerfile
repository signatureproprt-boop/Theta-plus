FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 THETA_HOST=0.0.0.0
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home theta
COPY --chown=theta:theta *.py ./
COPY --chown=theta:theta static/ ./static/
USER theta
EXPOSE 8080
CMD ["python", "run.py"]
