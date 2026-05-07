import os
import argparse
import torch
import soundfile as sf
from datetime import timedelta
from pyannote.audio import Pipeline
from silero_vad import collect_chunks, get_speech_timestamps, load_silero_vad
from transformers import WhisperProcessor, WhisperForConditionalGeneration
import numpy as np

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

THRESHOLD = 0.05
MIN_SPEECH_DURATION_MS = 500 # 250
MIN_SILENCE_DURATION_MS = 300 # 100
SPEECH_PAD_MS = 250 # 120
CHUNK_LENGTH = 30 # time in second
MODEL_REMOTE = "openai/whisper-large-v3"
MODEL_LOCAL = "./whisper-large-v3-ja-final"

TEMPERATURE = 0.2 # 0.6
NUM_BEAMS = 10
REPETITION_PENALTY = 1.2
NO_REPEAT_NGRAM_SIZE = 3

INITIAL_PROMPT = (
    "以下は富岳NEXT開発プロジェクトの日本語の会議録です。"
    "固有名詞：理化学研究所、富岳、富士通、NVIDIA、R-CCS、BenchKit、Benchpark、"
    "富岳NEXT、GENESIS、SALMON、Spack、Ramble、OpenOnDemand、"
    "Wahib、Domke、Dawson、近藤、佐野、井上、青木、小林、西澤、中村、"
    "専門用語：コデザイン、ベンチマーク、フレームワーク、スーパーコンピュータ、"
    "ワーキンググループ、アーキテクチャ、フラッグシップ、生成AI、知識蒸留。"
    "話者の発言を正確に書き起こしてください。"
)  # 222 tokens (上限 224)


def remove_silence(audio_file, sampling_rate, device=None):
    print("[INFO] Detecting silent segments (Silero VAD)...")
    model = load_silero_vad(onnx=False)
    if device is not None:
        model = model.to(device)
    # silero_vad.read_audio は torchaudio.load を使うため soundfile で代替する。
    # ffmpegで16kHz monoに変換済みのWAVを受け取る前提。
    audio_np, sr = sf.read(audio_file, dtype="float32")
    if audio_np.ndim > 1:
        audio_np = audio_np.mean(axis=1)
    audio = torch.from_numpy(audio_np)
    if device is not None:
        audio = audio.to(device)
    speech_timestamps = get_speech_timestamps(
        audio,
        model,
        sampling_rate=sampling_rate,
        threshold=THRESHOLD,
        min_speech_duration_ms=MIN_SPEECH_DURATION_MS,
        min_silence_duration_ms=MIN_SILENCE_DURATION_MS,
        speech_pad_ms=SPEECH_PAD_MS,
    )
    processed_audio = collect_chunks(speech_timestamps, audio)
    return processed_audio, sampling_rate, speech_timestamps


def vad_to_original_time(vad_start, vad_end, speech_timestamps, sample_rate):
    """VAD後音声のサンプル範囲 [vad_start, vad_end) を元音声上の秒数に変換する。"""
    pos = 0  # VAD後音声における現在位置（サンプル数）
    orig_start = orig_end = None

    for ts in speech_timestamps:
        seg_len = ts['end'] - ts['start']
        seg_vad_end = pos + seg_len

        if seg_vad_end > vad_start and pos < vad_end:
            offset_start = max(vad_start, pos) - pos
            offset_end = min(vad_end, seg_vad_end) - pos
            if orig_start is None:
                orig_start = ts['start'] + offset_start
            orig_end = ts['start'] + offset_end

        pos += seg_len

    if orig_start is None:  # フォールバック（通常は発生しない）
        orig_start, orig_end = vad_start, vad_end

    return orig_start / sample_rate, orig_end / sample_rate


def chunk_audio(audio, sample_rate, speech_timestamps, chunk_length_sec=30):
    if audio.ndim == 1:
        audio = audio.unsqueeze(0)
    chunk_size = chunk_length_sec * sample_rate
    total_length = audio.shape[-1]
    chunks = []
    for i in range(0, total_length, chunk_size):
        chunk = audio[:, i:i + chunk_size]
        start_sec, end_sec = vad_to_original_time(i, i + chunk.shape[-1], speech_timestamps, sample_rate)
        chunks.append((start_sec, end_sec, chunk))
    print(f"[INFO] Audio split into {len(chunks)} chunk(s) of up to {chunk_length_sec}s "
          f"(total {total_length} samples @ {sample_rate}Hz)")
    return chunks


def load_model(use_local, hf_token, device):
    if use_local:
        print(f"[INFO] Loading Whisper model from local directory: {MODEL_LOCAL}")
        processor = WhisperProcessor.from_pretrained(MODEL_LOCAL, language="Japanese", task="transcribe")
        model = WhisperForConditionalGeneration.from_pretrained(
            MODEL_LOCAL, torch_dtype=torch.float16
        ).to(device)
    else:
        print(f"[INFO] Loading Whisper model from Hugging Face: {MODEL_REMOTE}")
        processor = WhisperProcessor.from_pretrained(MODEL_REMOTE, token=hf_token, language="ja", task="transcribe")
        model = WhisperForConditionalGeneration.from_pretrained(
            MODEL_REMOTE, token=hf_token, torch_dtype=torch.float16
        ).to(device)
    model.eval()
    return processor, model


def transcribe_chunks(chunks, processor, model, device):
    print("[INFO] Transcribing chunks (Whisper)...")
    segments = []
    for start_sec, end_sec, chunk in chunks:
        print(f"[INFO] Transcribing chunk {start_sec}s - {end_sec}s", flush=True)
        inputs = processor(
            chunk.squeeze().cpu().numpy(),
            sampling_rate=16000,
            return_tensors="pt",
            language="ja",
            task="transcribe",
            initial_prompt=INITIAL_PROMPT,
        )
        input_features = inputs.input_features.to(device, dtype=model.dtype)

        with torch.no_grad():
            generated_ids = model.generate(
                input_features,
                return_timestamps=True,
                temperature=TEMPERATURE,
                do_sample=TEMPERATURE > 0,
                num_beams=NUM_BEAMS,
                repetition_penalty=REPETITION_PENALTY,
                no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
            )

        result = processor.batch_decode(generated_ids, skip_special_tokens=True)
        transcription = result[0].encode("utf-8", errors="ignore").decode("utf-8").strip()
        print(transcription, flush=True)

        segments.append({"start": start_sec, "end": end_sec, "text": transcription})
    return segments


def assign_speaker_labels(segments, diarization):
    labeled_segments = []
    for seg in segments:
        whisper_start = seg['start']
        whisper_end = seg['end']
        text = seg['text']
        if not text or text in ["...", "…"]:
            continue
        speaker = "UNKNOWN"
        for turn, _, spk in diarization.itertracks(yield_label=True):
            if whisper_start < turn.end and whisper_end > turn.start:
                speaker = spk
                break
        labeled_segments.append({
            "start": whisper_start,
            "end": whisper_end,
            "speaker": speaker,
            "text": text,
        })
    return labeled_segments


def write_output(output_path, labeled_segments):
    print(f"[INFO] Writing output: {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Transcription\n\n")

        prev_speaker = None
        prev_start = prev_end = None
        buffer = ""

        for seg in labeled_segments:
            start = int(seg['start'])
            end = int(seg['end'])
            speaker = seg['speaker']
            text = seg['text'].strip()

            if speaker == prev_speaker:
                buffer += "\n" + text
                prev_end = end
            else:
                if prev_speaker is not None:
                    s = str(timedelta(seconds=prev_start))
                    e = str(timedelta(seconds=prev_end))
                    f.write(f"#### [{s} - {e}] {prev_speaker}\n{buffer.strip()}\n\n")
                prev_speaker = speaker
                prev_start = start
                prev_end = end
                buffer = text

        if prev_speaker is not None:
            s = str(timedelta(seconds=prev_start))
            e = str(timedelta(seconds=prev_end))
            f.write(f"#### [{s} - {e}] {prev_speaker}\n{buffer.strip()}\n\n")


def main():
    parser = argparse.ArgumentParser(description="Transcription with Whisper + PyAnnote + Silero VAD")
    parser.add_argument("input_audio", help="Input audio file path (e.g., meeting.wav)")
    parser.add_argument("output_text", help="Output text file path (e.g., result.txt)")
    parser.add_argument("--local", action="store_true", help=f"Use local fine-tuned model ({MODEL_LOCAL})")
    args = parser.parse_args()

    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"[INFO] GPU: {torch.cuda.get_device_name(0)}")
    else:
        print("[WARNING] CUDA not available, falling back to CPU (this will be very slow)")
        device = torch.device("cpu")
    hf_token = os.getenv("HUGGING_FACE_TOKEN")

    # Silero VAD はCPUで実行する（vLLMがGPUメモリを大量確保しているため）
    processed_waveform, sample_rate, speech_timestamps = remove_silence(args.input_audio, sampling_rate=16000, device=None)
    chunks = chunk_audio(processed_waveform, sample_rate, speech_timestamps, chunk_length_sec=CHUNK_LENGTH)

    print("[INFO] Running speaker diarization (PyAnnote)...")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                        token=hf_token).to(device)
    audio_np, sr = sf.read(args.input_audio, dtype="float32", always_2d=True)
    original_waveform = torch.from_numpy(audio_np.T)  # (channels, samples)
    diarization = pipeline({"waveform": original_waveform, "sample_rate": sr})
    # 新しい pyannote は DiarizeOutput を返す。Annotation を取り出す。
    if not hasattr(diarization, "itertracks"):
        diarization = diarization.speaker_diarization

    processor, model = load_model(args.local, hf_token, device)
    segments = transcribe_chunks(chunks, processor, model, device)
    labeled = assign_speaker_labels(segments, diarization)
    write_output(args.output_text, labeled)

    print("[INFO] Finished")


if __name__ == "__main__":
    main()
