# ---------------------------------------------------------------------------
# Java business backend: the only service exposed to the frontend (8000).
# AI work is delegated to the internal ai-service (see docker-compose.yml).
# ---------------------------------------------------------------------------
FROM maven:3.9-eclipse-temurin-21 AS build

WORKDIR /build
COPY pom.xml .
# Cache dependencies before the sources; go-offline is best-effort only.
RUN mvn -q dependency:go-offline || true
COPY src src
RUN mvn -q package -DskipTests

FROM eclipse-temurin:21-jre

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /build/target/*.jar app.jar

ENV DATA_DIR=/app/data \
    JAVA_OPTS=""

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["sh", "-c", "java $JAVA_OPTS -jar /app/app.jar"]
