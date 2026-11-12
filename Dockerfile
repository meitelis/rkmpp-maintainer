FROM docker:29.9.3-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d
RUN apk add --no-cache python3 git ca-certificates bash
WORKDIR /opt/maintainer
COPY *.py config.json ./
COPY tests ./tests
RUN python3 -m unittest discover -s tests -v
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/opt/maintainer/maintainer.py"]
CMD ["check"]
