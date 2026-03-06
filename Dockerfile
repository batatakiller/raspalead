FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Copiar apenas os requirements primeiro para otimizar o cache do Docker
COPY requirements.txt .

# Atualizar pip e instalar pacotes Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Criar diretorio para o sqlite e imagens persistentes
RUN mkdir -p /app/data

# Definir como um volume para que a VPS nao perca os dados ao recriar o container
VOLUME ["/app/data"]

# Copiar a aplicação
COPY app.py .

EXPOSE 8501

# Rodar a aplicacao via Streamlit
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
