#!/bin/bash

echo "🚀 Bắt đầu cài đặt môi trường FULL POWER cho Vast.ai (Avatar + LLM + TTS + WebUI)..."

# 1. Cài đặt các thư viện hệ thống cần thiết (Dùng root trực tiếp)
apt-get update
apt-get install -y ffmpeg espeak-ng libespeak-ng-dev build-essential python3-dev \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 curl git

# 2. Cài đặt công cụ UV (Siêu tốc độ)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env

# 3. Cài đặt Python dependencies cho dự án chính
echo "📦 Đang cài đặt thư viện cho LiveTalking..."
pip install --upgrade pip
pip install -r requirements.txt
pip install "lmdeploy[all]" vieneu resampy soundfile openai

# 3b. Cài PyTorch cu128 (Blackwell / RTX 50x cần sm_120 — chỉ có trong torch>=2.7).
# Cài SAU lmdeploy để override torch pin cũ mà lmdeploy có thể kéo theo.
# Nếu không có 50x, torch 2.7 + cu128 vẫn chạy tốt trên 30x/40x (sm_86/sm_89).
pip install --upgrade --force-reinstall \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu128

# 3c. onnxruntime-gpu phải khớp CUDA 12.x (default pip kéo build CUDA 11).
pip install --upgrade "onnxruntime-gpu>=1.20.0"

# 4. Tải Model Wav2Lip cho Avatar
echo "📥 Đang tải Model Wav2Lip..."
mkdir -p models
if [ ! -f "models/wav2lip.pth" ]; then
    wget https://github.com/lipku/LiveTalking/releases/download/v1.0/wav2lip.pth -O models/wav2lip.pth
fi

# 5. Khởi chạy LLM Server (LMDeploy TurboMind - Cổng 11434)
echo "🤖 Đang khởi chạy LLM Server (Qwen-2.5-7B)..."
nohup lmdeploy serve api_server Qwen/Qwen2.5-7B-Instruct \
    --server-name 0.0.0.0 --server-port 11434 \
    --model-name qwen2.5 \
    --cache-max-entry-count 0.4 > llm_server.log 2>&1 &

# 6. Khởi chạy VieNeu-TTS API Server (LMDeploy v2 - Cổng 23333)
echo "🔊 Đang khởi chạy VieNeu-TTS-v2 API Server..."
nohup lmdeploy serve api_server pnnbao-ump/VieNeu-TTS-v2 \
    --server-name 0.0.0.0 --server-port 23333 \
    --model-name pnnbao-ump/VieNeu-TTS-v2 \
    --cache-max-entry-count 0.2 > vieneu_server.log 2>&1 &

# 7. Khởi chạy VieNeu Web UI (Giao diện quản trị - Cổng 7860)
echo "🌐 Đang thiết lập VieNeu Web UI..."
if [ ! -d "VieNeu-TTS" ]; then
    git clone https://github.com/pnnbao97/VieNeu-TTS.git
fi
cd VieNeu-TTS
# Khởi chạy Web UI ở background
nohup uv run vieneu-web --server-name 0.0.0.0 --server-port 7860 > webui.log 2>&1 &
cd ..

echo "✅ TẤT CẢ HỆ THỐNG ĐÃ SẴN SÀNG!"
echo "-------------------------------------------------------"
echo "BẢN ĐỒ CỔNG TRÊN VAST.AI:"
echo "- 8010 : Giao diện Livestream (Avatar)"
echo "- 7860 : Giao diện Quản trị Giọng nói (Web UI)"
echo "- 23333: API VieNeu-TTS (Đang chạy)"
echo "- 11434: API LLM Qwen (Đang chạy)"
echo "-------------------------------------------------------"
echo "Lệnh khởi chạy livestream:"
echo "python app.py --tts vienuetts --TTS_SERVER http://localhost:23333/v1 --llm_url http://localhost:11434/v1 --llm_model qwen2.5 --REF_FILE Ly"
echo "-------------------------------------------------------"
