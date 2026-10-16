# Read the doc: https://huggingface.co/docs/hub/spaces-sdks-docker
# you will also find guides on how best to write your Dockerfile

FROM python:3.11

RUN useradd -m -u 1000 user
USER user
ENV HOME="/home/user" \
    PATH="/home/user/.local/bin:$PATH"

WORKDIR /home/user/app

COPY --chown=user ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --user --upgrade -r requirements.txt

COPY --chown=user . .

# main.py reads $PORT (Railway sets it) and falls back to 7860 (Hugging Face default)
CMD ["python", "main.py"]
