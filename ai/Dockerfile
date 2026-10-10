FROM python:3.12-slim
RUN pip install --no-cache-dir discord.py==2.5.2
WORKDIR /app
COPY bot.py prompt.txt test_bot.py ./
CMD ["python", "-u", "bot.py"]
