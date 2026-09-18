@echo off
setlocal
title Setup Bot Maxima
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYTHON_CMD=py -3.11"

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if %errorlevel%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo.
    echo Python 3.11 nao encontrado.
    echo Instale o Python 3.11 e execute novamente este script.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Criando virtualenv .venv...
    call %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo Falha ao criar a virtualenv.
        pause
        exit /b 1
    )
)

echo.
echo Atualizando pip...
.\.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 (
    echo Falha ao atualizar o pip.
    pause
    exit /b 1
)

echo.
echo Instalando dependencias...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Falha ao instalar as dependencias.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo Criando .env a partir de .env.example...
    copy /Y ".env.example" ".env" >nul
)

if not exist "documentos" (
    mkdir documentos >nul 2>nul
)

echo.
echo Setup concluido.
echo Proximos passos:
echo  1. Preencha o arquivo .env com as chaves reais e a DATABASE_URL.
echo  2. Em Docker, suba primeiro o servico postgres: docker compose up -d postgres
echo  3. Rode a ingestao inicial: docker compose --profile tools run --rm ingest
echo  4. Para Discord: iniciar.bat
echo.
pause
