import glob
import os
import shutil
import subprocess
import tempfile

import uuid

import modal
from pydantic import BaseModel

app = modal.App("hallo3-portrait-avatar")

volume = modal.Volume.from_name("hallo3-cache", create_if_missing=True)

volumes = {"/models": volume}


def download_hallo3_models():
    from huggingface_hub import snapshot_download
    snapshot_download(
        "fudan-generative-ai/hallo3",
        local_dir="/models/pretrained_models",
        ignore_patterns=[]
    )


image = (
    modal.Image
    .from_registry("nvidia/cuda:12.1.1-devel-ubuntu20.04", add_python="3.10")
    .pip_install_from_pyproject("photo-to-video/pyproject.toml")
    .env({
        "DEBIAN_FRONTEND": "noninteractive",
        "CC": "gcc",
        "CXX": "g++",
        "CUDA_HOME": "/usr/local/cuda",
        "PATH": "/usr/local/cuda/bin:$PATH",
        "CFLAGS": "-O2",
        "CXXFLAGS": "-O2"
    })
    .apt_install("git", "ffmpeg", "libaio-dev", "libgl1-mesa-glx", "libglib2.0-0", "libsm6", "libxext6", "libxrender-dev", "libgomp1", "python3-dev", "build-essential", "g++", "make", "cmake")
    .run_commands("pip install numpy cython setuptools wheel")
    .run_commands("pip install insightface==0.7.3")
    .run_commands("pip install onnxruntime-gpu")
    .run_commands("git clone https://github.com/fudan-generative-vision/hallo3 /hallo3")
    .run_commands("ln -s /models/pretrained_models /hallo3/pretrained_models")
    .run_function(download_hallo3_models, volumes=volumes)
)

s3_secret = modal.Secret.from_name("perzona-secret")


class PortraitAvatarRequest(BaseModel):
    transcript: str
    photo_s3_key: str
    audio_s3_key: str


class PortraitAvatarResponse(BaseModel):
    video_s3_key: str


@app.cls(
    image=image,
    gpu="A100-80GB",
    volumes={
        **volumes,
        "/s3-mount": modal.CloudBucketMount("perzona-bucket", secret=s3_secret)
    },
    timeout=2700,
    secrets=[s3_secret]
)
class PortraitAvatarServer:
    @modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
    def generate_video(self, request: PortraitAvatarRequest) -> PortraitAvatarResponse:
        print(
            f"Received request to generate portrait avatar: {request.transcript} {request.photo_s3_key} {request.audio_s3_key}")

        temp_dir = tempfile.mkdtemp()

        try:
            photo_path = f"/s3-mount/{request.photo_s3_key}"
            audio_path = f"/s3-mount/{request.audio_s3_key}"

            print(f"Checking photo path: {photo_path}")
            print(f"Checking audio path: {audio_path}")
            
            if not os.path.exists(photo_path):
                print(f"Photo file not found at {photo_path}")
                print(f"S3 mount directory contents: {os.listdir('/s3-mount') if os.path.exists('/s3-mount') else 'S3 mount not found'}")
                raise FileNotFoundError(f"Photo not found at {photo_path}")
            if not os.path.exists(audio_path):
                print(f"Audio file not found at {audio_path}")
                print(f"S3 mount directory contents: {os.listdir('/s3-mount') if os.path.exists('/s3-mount') else 'S3 mount not found'}")
                raise FileNotFoundError(f"Audio not found at {audio_path}")
                
            print(f"Photo file size: {os.path.getsize(photo_path)} bytes")
            print(f"Audio file size: {os.path.getsize(audio_path)} bytes")

            input_txt_path = os.path.join(temp_dir, "input.txt")
            with open(input_txt_path, "w") as f:
                f.write(f"{request.transcript}@@{photo_path}@@{audio_path}\n")

            print("Generating video with Hallo3")
            output_dir = os.path.join(temp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)

            command = [
                "bash",
                "/hallo3/scripts/inference_long_batch.sh",
                input_txt_path,
                output_dir
            ]

            print(f"Running command: {' '.join(command)}")
            print(f"Working directory: /hallo3")
            print(f"Input file exists: {os.path.exists(input_txt_path)}")
            print(f"Input file content: {open(input_txt_path, 'r').read()}")
            
            try:
                result = subprocess.run(command, check=True, cwd="/hallo3", 
                                      capture_output=True, text=True, timeout=2700)
                print(f"Command stdout: {result.stdout}")
                print(f"Command stderr: {result.stderr}")
            except subprocess.TimeoutExpired:
                print("Command timed out after 5 minutes")
                raise RuntimeError("Hallo3 inference timed out")
            except subprocess.CalledProcessError as e:
                print(f"Command failed with return code {e.returncode}")
                print(f"stdout: {e.stdout}")
                print(f"stderr: {e.stderr}")
                raise RuntimeError(f"Hallo3 inference failed: {e.stderr}")
            
            print("Processing finished")
            print(f"Output directory contents: {os.listdir(output_dir)}")
            
            # Check for any video files in the output directory
            all_files = []
            for root, dirs, files in os.walk(output_dir):
                for file in files:
                    all_files.append(os.path.join(root, file))
            print(f"All files in output directory: {all_files}")

            generated_video_file = None
            for fpath in glob.glob(os.path.join(output_dir, "**", "*.mp4"), recursive=True):
                generated_video_file = fpath
                print(f"Found video file: {fpath}")
                break
                
            if not generated_video_file:
                # Try to find any video file with different extensions
                for ext in ["*.avi", "*.mov", "*.mkv", "*.webm"]:
                    for fpath in glob.glob(os.path.join(output_dir, "**", ext), recursive=True):
                        generated_video_file = fpath
                        print(f"Found video file with extension {ext}: {fpath}")
                        break
                    if generated_video_file:
                        break
                        
            if not generated_video_file:
                error_msg = f"Hallo3 did not produce a video file. Output directory contents: {os.listdir(output_dir)}"
                print(error_msg)
                raise RuntimeError(error_msg)

            print("Merging the video and audio with ffmpeg.")
            final_video_path = os.path.join(temp_dir, "final_video.mp4")
            ffmpeg_command = [
                "ffmpeg",
                "-i", generated_video_file,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_video_path
            ]

            subprocess.run(ffmpeg_command, check=True)
            print(f"Final video created at {final_video_path}")

            video_uuid = str(uuid.uuid4())
            s3_key = f"ptv/{video_uuid}.mp4"
            s3_path = f"/s3-mount/{s3_key}"
            os.makedirs(os.path.dirname(s3_path), exist_ok=True)
            shutil.copy(final_video_path, s3_path)
            print(f"Saved video to S3: {s3_key}")

            return PortraitAvatarResponse(video_s3_key=s3_key)
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)


@app.local_entrypoint()
def main():
    import requests

    server = PortraitAvatarServer()
    endpoint_url = server.generate_video.get_web_url()

    request = PortraitAvatarRequest(
        transcript="",
        photo_s3_key="samples/photos/0008.jpg",
        audio_s3_key="samples/voices/1.wav"
    )

    payload = request.model_dump()

    headers = {
        "Modal-Key": "wk-yIWcTVBbpWqSss2VNv89Lu",
        "Modal-Secret": "ws-1GVvYuGPUWcYYBEuY7WhuA"
    }

    response = requests.post(endpoint_url, json=payload, headers=headers)
    response.raise_for_status()

    result = PortraitAvatarResponse(**response.json())

    print(result.video_s3_key)