FROM alpine:3.15 as build
# Download helm binary, use extra stage so we do not need to install curl/wget in final image
RUN wget -O - https://get.helm.sh/helm-v3.8.1-linux-amd64.tar.gz | tar -xzO linux-amd64/helm > /helm

FROM python:3.10-slim

COPY --from=build /helm /usr/local/bin/helm
RUN mkdir /operator
WORKDIR /operator
# Install python dependencies
COPY requirements.txt /operator/requirements.txt
RUN pip install -r /operator/requirements.txt
# Download helm charts
RUN chmod +x /usr/local/bin/helm \
    && helm repo add bitnami https://charts.bitnami.com/bitnami \
    && helm pull bitnami/postgresql --untar --version 10.16.1 --destination charts
# Copy operator code
COPY main.py /operator/
COPY hybridcloud /operator/hybridcloud
CMD ["kopf", "run", "--liveness=http://0.0.0.0:8080/healthz", "main.py", "-A"]
