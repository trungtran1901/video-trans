# Video Translator & Dubbing

Ứng dụng dịch phụ đề video bằng LLM (dịch tự nhiên, không word-by-word), hỗ trợ:
- Gắn phụ đề cứng (burn-in) hoặc phụ đề mềm (soft sub) vào video
- Xuất file `.srt` riêng
- Lồng tiếng (dubbing) thay thế audio gốc bằng TTS đa ngôn ngữ
- Dịch sang nhiều ngôn ngữ cùng lúc
- Cấu hình LLM theo chuẩn OpenAI-compatible API (đổi được `base_url`, `api_key`, `model` — dùng được với OpenAI, Azure OpenAI, Ollama, LM Studio, vLLM, OpenRouter...)

## Chạy local (development)

Yêu cầu: Python 3.11+, ffmpeg đã cài trong PATH.

```bash
chmod +x run.sh
./run.sh
```

Mở trình duyệt tại `http://localhost:8000`.

## Chạy dạng ứng dụng Windows (desktop, không cần trình duyệt)

Bản này mở một cửa sổ ứng dụng Windows thật (dùng engine Edge WebView2 có sẵn trên Windows 10/11), không cần mở trình duyệt hay chạy Docker. Chọn video vẫn dùng đúng hộp thoại "Open File" gốc của Windows.

### Chạy thử (development, chưa đóng gói)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-desktop.txt
python desktop_app.py
```

Yêu cầu máy đã cài `ffmpeg`/`ffprobe` trong PATH (như bản web), hoặc đặt `ffmpeg.exe`/`ffprobe.exe` vào thư mục `ffmpeg\bin\` cạnh `desktop_app.py` — app sẽ tự nhận nếu có.

### Đóng gói thành file .exe

```bash
build_windows.bat
```

Kết quả nằm ở `dist\VideoTranslator\VideoTranslator.exe`. Sau khi build xong:

1. Copy thư mục `ffmpeg\bin\` (chứa `ffmpeg.exe`, `ffprobe.exe`) vào bên trong `dist\VideoTranslator\` để không phụ thuộc PATH của máy người dùng cuối.
2. Nếu muốn người dùng cuối không phải tự tải model lần đầu, chạy `python scripts/prefetch_models.py` trước khi build, rồi copy nguyên thư mục cache (đường dẫn in ra bởi `--show-paths`) vào cùng `dist\VideoTranslator\` và trỏ biến môi trường `HF_HOME`/tương đương trong `desktop_app.py`, hoặc đơn giản hơn là hướng dẫn người dùng để máy tự tải ở lần chạy đầu (cần mạng ổn định).

**Lưu ý quan trọng:**
- Bộ cài sẽ khá nặng (nhiều GB) do `torch`, `TTS`, `faster-whisper` đi kèm.
- Máy chạy `.exe` cần có sẵn **Microsoft Edge WebView2 Runtime** — mặc định có sẵn trên Windows 10 (bản mới)/Windows 11; nếu thiếu, Windows sẽ tự nhắc cài hoặc tải tại: https://developer.microsoft.com/microsoft-edge/webview2/
- Đây là bản build "single-folder" (một thư mục chứa `.exe` + toàn bộ thư viện), không phải "single-file" — do kích thước quá lớn nếu gộp 1 file sẽ giải nén rất chậm mỗi lần mở app. Muốn phân phối cho người khác, nén cả thư mục `dist\VideoTranslator\` thành `.zip`.
- File cấu hình LLM, job, video/output vẫn lưu trong thư mục `data\` cạnh file `.exe` (giống hành vi bản local/Docker).

## Chạy bằng Docker (khuyến nghị cho deploy)

```bash
docker compose up --build -d
```

Ứng dụng chạy tại `http://<server-ip>:8000`. Dữ liệu (video, output, cấu hình LLM) được lưu trong volume `video_translator_data`, không mất khi container restart.

### Build thủ công không dùng compose

```bash
docker build -t video-translator .
docker run -d -p 8000:8000 -v vt_data:/data --name video-translator video-translator
```

### Tải sẵn model TTS/ASR trước khi chạy (khuyến nghị cho mạng không ổn định)

Model nhận diện giọng nói (faster-whisper) và model lồng tiếng (Coqui XTTS v2) sẽ tự tải về từ Hugging Face trong lần chạy đầu tiên. Nếu mạng chặn/không ổn định với các CDN đó, tải trước trực tiếp trên máy host (dùng venv Python đã cài `requirements.txt`, không cần Docker cho bước này):

```bash
python scripts/prefetch_models.py
```

Script sẽ tải model vào đúng thư mục cache mặc định của máy bạn (ví dụ trên Windows: `%USERPROFILE%\.cache\huggingface` và `%LOCALAPPDATA%\tts`), đồng thời in ra đường dẫn chính xác ở cuối.

Tạo file `.env` ở thư mục gốc dự án (cùng cấp `docker-compose.yml`) với 2 dòng đó, ví dụ:

```
HF_CACHE_DIR=C:/Users/yourname/.cache/huggingface
TTS_CACHE_DIR=C:/Users/yourname/AppData/Local/tts
```

`docker-compose.yml` sẽ tự đọc file `.env` này và mount đúng 2 thư mục đó vào container — service chính dùng lại model đã tải, không cần gọi mạng ra ngoài nữa cho phần ASR/TTS. Nếu không tạo `.env`, mặc định sẽ dùng `./model-cache/huggingface` và `./model-cache/tts` ngay trong thư mục dự án (tự tải lại lần đầu chạy container nếu chưa có gì trong đó).

Muốn xem trước đường dẫn cache mà không tải gì cả:

```bash
python scripts/prefetch_models.py --show-paths
```

## Cấu hình LLM

Vào giao diện web → nút "Cấu hình LLM" ở góc trên, nhập:
- **Base URL**: mặc định `https://api.openai.com/v1`, đổi sang endpoint provider khác nếu cần
- **API Key**
- **Model**: gõ tay hoặc bấm "Lấy danh sách" để tự động fetch từ `/models` endpoint của provider
- Bấm "Kiểm tra kết nối" để test trước khi lưu

Cấu hình được lưu tại `/data/config/llm_config.json` (trong container) hoặc `./data/config/llm_config.json` (chạy local).

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WHISPER_MODEL_SIZE` | medium | Kích thước model faster-whisper (tiny/base/small/medium/large-v3) |
| `WHISPER_DEVICE` | cpu | `cpu` hoặc `cuda` nếu có GPU |
| `WHISPER_COMPUTE_TYPE` | int8 | Kiểu tính toán, dùng `float16` nếu chạy GPU |
| `TTS_MODEL_NAME` | xtts_v2 | Model TTS dùng cho lồng tiếng |
| `MAX_UPLOAD_SIZE_MB` | 2048 | Giới hạn dung lượng upload |

## Ghi chú triển khai production

- Nên đặt reverse proxy (nginx/Caddy) phía trước để có HTTPS và giới hạn upload size ở tầng proxy.
- Nếu chạy GPU cho ASR/TTS nhanh hơn, dùng base image `nvidia/cuda` thay vì `python:3.11-slim` và cài PyTorch bản CUDA tương ứng, đặt `WHISPER_DEVICE=cuda`.
- Xử lý job hiện chạy bằng thread nền trong cùng process — với tải lớn/nhiều người dùng nên tách sang hàng đợi riêng (Celery + Redis) và scale worker độc lập với web server.
- File job lưu tại `/data/jobs.json`, video/audio/srt output lưu tại `/data/outputs/<job_id>/`.

## Cấu trúc thư mục

```
app/
  core/       # ASR, dịch LLM, subtitle, TTS/dubbing, media ffmpeg, pipeline, job store
  api/        # FastAPI routes
  web/        # giao diện web (HTML/CSS/JS)
  main.py     # entrypoint FastAPI
Dockerfile
docker-compose.yml
requirements.txt
run.sh
```