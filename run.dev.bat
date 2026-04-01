@echo off
setlocal

set "COMPOSE_FILE=.\docker\docker-compose.dev.yml"
set "SERVICE=amy-dev"
set "IMAGE_NAME=amy_docker"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyMMdd"') do set "DATE_TAG=%%i"

docker compose -f "%COMPOSE_FILE%" build "%SERVICE%"
if errorlevel 1 exit /b %errorlevel%

docker compose -f "%COMPOSE_FILE%" up -d "%SERVICE%"
if errorlevel 1 exit /b %errorlevel%

set "IMAGE_ID="
for /f %%i in ('docker compose -f "%COMPOSE_FILE%" images -q "%SERVICE%"') do set "IMAGE_ID=%%i"

if not defined IMAGE_ID (
    echo Failed to resolve image ID for service "%SERVICE%".
    exit /b 1
)

docker tag "%IMAGE_ID%" "%IMAGE_NAME%"
if errorlevel 1 exit /b %errorlevel%

docker tag "%IMAGE_ID%" "%IMAGE_NAME%:%DATE_TAG%"
if errorlevel 1 exit /b %errorlevel%

echo Tagged %IMAGE_NAME% and %IMAGE_NAME%:%DATE_TAG%
