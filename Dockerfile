FROM ubuntu:22.04

# 非対話モードに設定
ENV DEBIAN_FRONTEND=noninteractive
ENV PJSIP_VERSION=2.16

# 依存関係のインストール
RUN apt-get update && apt-get install -y \
    build-essential \
    python3 \
    python3-dev \
    python3-pip \
    python-is-python3 \
    wget \
    swig \
    libssl-dev \
    libopus-dev \
    libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

# PJSIPをダウンロード＆ビルド (pjsua2対応)
WORKDIR /tmp
RUN wget https://github.com/pjsip/pjproject/archive/refs/tags/$PJSIP_VERSION.tar.gz && \
    tar xzf $PJSIP_VERSION.tar.gz && \
    cd pjproject-$PJSIP_VERSION && \
    ./configure --enable-shared --disable-video --disable-sound --with-pjsua2 CFLAGS="-fPIC -O2" CXXFLAGS="-fPIC -O2" && \
    make dep && make && make install && \
    ldconfig && \
    cd /tmp/pjproject-$PJSIP_VERSION/pjsip-apps/src/swig/python && \
    make && \
    make install && \
    echo "=== Trying to import pjsua2 ===" && \
    python3 -c "import pjsua2; ep = pjsua2.Endpoint(); ep.libCreate(); print('pjsua2 successfully imported!', ep.libVersion().full); ep.libDestroy()" && \
    cd /tmp && rm -rf /tmp/pjproject-$PJSIP_VERSION /tmp/$PJSIP_VERSION.tar.gz

# アプリケーションディレクトリ
WORKDIR /app

# Python依存関係をインストール
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# アプリケーションをコピー
COPY . .

# 実行
CMD ["python3", "main.py"]
