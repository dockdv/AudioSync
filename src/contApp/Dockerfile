# ---- build stage ----
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

# Copy solution and project files first for layer caching
COPY src/webGUI/AudioSync.slnx ./
COPY src/webGUI/src/AudioSync.Web/AudioSync.Web.csproj src/AudioSync.Web/
COPY src/webGUI/src/AudioSync.Core/AudioSync.Core.csproj src/AudioSync.Core/
COPY src/webGUI/tests/AudioSync.Core.Tests/AudioSync.Core.Tests.csproj tests/AudioSync.Core.Tests/
RUN dotnet restore src/AudioSync.Web/AudioSync.Web.csproj

# Copy the rest and publish
COPY src/webGUI/ ./
RUN dotnet publish src/AudioSync.Web/AudioSync.Web.csproj \
        -c Release \
        -o /app/publish \
        --no-self-contained

# ---- runtime stage ----
FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg mkvtoolnix curl && \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi && \
    rm -f /usr/bin/ffplay && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /app/publish ./

RUN mkdir -p /app/uploads

ENV ASPNETCORE_URLS=http://0.0.0.0:5000 \
    ASPNETCORE_ENVIRONMENT=Production

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:5000/ || exit 1

ENTRYPOINT ["dotnet", "AudioSync.Web.dll"]
