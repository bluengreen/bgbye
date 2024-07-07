from fastapi import FastAPI, UploadFile, File, Response, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image
import io
import shutil
from rembg import remove as rembg_remove, new_session
import time
import numpy as np
import tempfile
import uuid  
import os
import subprocess
from transformers import pipeline
from transparent_background import Remover
import logging
import asyncio
from datetime import datetime, timedelta
import torch

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

# Create temp_videos folder if it doesn't exist
TEMP_VIDEOS_DIR = "temp_videos"
os.makedirs(TEMP_VIDEOS_DIR, exist_ok=True)

# Create a frames directory within temp_videos
FRAMES_DIR = os.path.join(TEMP_VIDEOS_DIR, "frames")
os.makedirs(FRAMES_DIR, exist_ok=True)

# Add a dictionary to store processing status
processing_status = {}

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def cleanup_old_videos():
    while True:
        current_time = datetime.now()
        for item in os.listdir(TEMP_VIDEOS_DIR):
            item_path = os.path.join(TEMP_VIDEOS_DIR, item)
            item_modified = datetime.fromtimestamp(os.path.getmtime(item_path))
            if current_time - item_modified > timedelta(minutes=10):
                if os.path.isfile(item_path):
                    os.remove(item_path)
                    logger.info(f"Removed old file: {item_path}")
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    logger.info(f"Removed old directory: {item_path}")
        await asyncio.sleep(600)  # Run every 10 minutes

# Pre-load all models
bria_model = pipeline("image-segmentation", model="briaai/RMBG-1.4", trust_remote_code=True, device="cpu")
inspyrenet_model = Remover()
inspyrenet_model.model.cpu()
rembg_models = {
    'u2net': new_session('u2net'),
    'u2net_human_seg': new_session('u2net_human_seg'),
    'isnet-general-use': new_session('isnet-general-use'),
    'isnet-anime': new_session('isnet-anime')
}

def process_with_bria(image):
    result = bria_model(image, return_mask=True)
    mask = result
    if not isinstance(mask, Image.Image):
        mask = Image.fromarray((mask * 255).astype('uint8'))
    no_bg_image = Image.new("RGBA", image.size, (0, 0, 0, 0))
    no_bg_image.paste(image, mask=mask)
    return no_bg_image

def process_with_inspyrenet(image):
    return inspyrenet_model.process(image, type='rgba')

def process_with_rembg(image, model='u2net'):
    return rembg_remove(image, session=rembg_models[model])

@app.post("/remove_background/")
async def remove_background(file: UploadFile = File(...), method: str = Form(...)):
    try:
        image = Image.open(io.BytesIO(await file.read())).convert('RGB')
        
        start_time = time.time()
        
        if method == 'bria':
            no_bg_image = process_with_bria(image)
        elif method == 'inspyrenet':
            inspyrenet_model.model.cuda()
            no_bg_image = process_with_inspyrenet(image)
            inspyrenet_model.model.cpu()
            torch.cuda.empty_cache()
        elif method in ['u2net', 'u2net_human_seg', 'isnet-general-use', 'isnet-anime']:
            no_bg_image = process_with_rembg(image, model=method)
        else:
            raise HTTPException(status_code=400, detail="Invalid method")
        
        process_time = time.time() - start_time
        print(f"Background removal time ({method}): {process_time:.2f} seconds")

        with io.BytesIO() as output:
            no_bg_image.save(output, format="PNG")
            content = output.getvalue()

        return Response(content=content, media_type="image/png")

    except Exception as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=str(e))

async def process_frame(frame_path, method):
    img = Image.open(frame_path).convert('RGB')
    
    if method == 'bria':
        processed_frame = await asyncio.to_thread(process_with_bria, img)
    elif method == 'inspyrenet':
        processed_frame = await asyncio.to_thread(process_with_inspyrenet, img)
    elif method in ['u2net', 'u2net_human_seg', 'isnet-general-use', 'isnet-anime']:
        processed_frame = await asyncio.to_thread(process_with_rembg, img, model=method)
    else:
        raise ValueError("Invalid method")
    
    return processed_frame

async def process_video(video_path, method, video_id):
    try:
        if method == 'inspyrenet':
            inspyrenet_model.model.cuda()

        processing_status[video_id] = {'status': 'processing', 'progress': 0, 'message': 'Initializing'}
        
        logger.info(f"Starting video processing: {video_path}")
        logger.info(f"Method: {method}")
        logger.info(f"Video ID: {video_id}")

        # Check video frame count
        frame_count_command = ['ffmpeg.ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', 
                               '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', video_path]
        process = await asyncio.create_subprocess_exec(
            *frame_count_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error counting frames: {stderr.decode()}")
            processing_status[video_id] = {'status': 'error', 'message': 'Error counting frames'}
            return

        frame_count = int(stdout.decode().strip())
        logger.info(f"Video frame count: {frame_count}")

        if frame_count > 250:
            logger.warning(f"Video too long: {frame_count} frames")
            processing_status[video_id] = {'status': 'error', 'message': 'Video too long (max 250 frames)'}
            return

        # Create a unique directory for this video's frames
        frames_dir = os.path.join(FRAMES_DIR, video_id)
        os.makedirs(frames_dir, exist_ok=True)
        logger.info(f"Created frames directory: {frames_dir}")

        # Extract frames from video
        processing_status[video_id] = {'status': 'processing', 'progress': 0, 'message': 'Extracting frames'}
        extract_command = ['ffmpeg', '-i', video_path, f'{frames_dir}/frame_%05d.png']
        logger.info(f"Executing frame extraction command: {' '.join(extract_command)}")
        process = await asyncio.create_subprocess_exec(
            *extract_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error extracting frames: {stderr.decode()}")
            processing_status[video_id] = {'status': 'error', 'message': 'Error extracting frames'}
            return

        # Process frames
        processing_status[video_id] = {'status': 'processing', 'progress': 0, 'message': 'Removing background'}
        frame_files = sorted([f for f in os.listdir(frames_dir) if f.endswith('.png')])
        total_frames = len(frame_files)
        logger.info(f"Number of extracted frames: {total_frames}")

        if total_frames == 0:
            logger.error("No frames were extracted from the video")
            processing_status[video_id] = {'status': 'error', 'message': 'No frames were extracted from the video'}
            return

        for i, frame_file in enumerate(frame_files):
            frame_path = os.path.join(frames_dir, frame_file)
            processed_frame = await process_frame(frame_path, method)
            processed_frame.save(frame_path, format='PNG')
            progress = (i + 1) / total_frames * 100
            processing_status[video_id] = {'status': 'processing', 'progress': progress}
            #logger.info(f"Processed frame {i+1}/{total_frames} ({progress:.2f}%)")


        # Create output video
        processing_status[video_id] = {'status': 'processing', 'progress': 100, 'message': 'Encoding video'}
        output_path = os.path.join(TEMP_VIDEOS_DIR, f"output_{video_id}.webm")
        create_video_command = [
            'ffmpeg',
            '-framerate', '24',
            '-i', f'{frames_dir}/frame_%05d.png',
            '-c:v', 'libvpx-vp9',
            '-pix_fmt', 'yuva420p',
            '-lossless', '1',
            output_path
        ]
        logger.info(f"Executing video creation command: {' '.join(create_video_command)}")
        process = await asyncio.create_subprocess_exec(
            *create_video_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error creating output video: {stderr.decode()}")
            processing_status[video_id] = {'status': 'error', 'message': 'Error creating output video'}
            return

        logger.info(f"Video processing completed. Output path: {output_path}")
        processing_status[video_id] = {'status': 'completed', 'output_path': output_path}

    except Exception as e:
        logger.exception("Error in video processing")
        processing_status[video_id] = {'status': 'error', 'message': str(e)}
        
        if method == 'inspyrenet':  
            inspyrenet_model.model.cpu()
            torch.cuda.empty_cache()

    finally:
        # Clean up frames directory
        for file in os.listdir(frames_dir):
            os.remove(os.path.join(frames_dir, file))
        os.rmdir(frames_dir)
        logger.info(f"Cleaned up frames directory: {frames_dir}")

        if method == 'inspyrenet':  
            inspyrenet_model.model.cpu()
            torch.cuda.empty_cache()

@app.post("/remove_background_video/")
async def remove_background_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), method: str = Form(...)):
    try:
        logger.info(f"Starting video background removal with method: {method}")
        
        # Generate a unique filename for the uploaded video
        video_id = str(uuid.uuid4())
        filename = f"input_{video_id}.mp4"
        file_path = os.path.join(TEMP_VIDEOS_DIR, filename)
        
        # Save uploaded video to the temp_videos folder
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        logger.info(f"Video file saved: {file_path}")
        logger.info(f"File exists: {os.path.exists(file_path)}")
        logger.info(f"File size: {os.path.getsize(file_path)} bytes")

        if not os.path.exists(file_path):
            raise HTTPException(status_code=500, detail=f"Failed to create video file: {file_path}")

        # Start processing in the background
        background_tasks.add_task(process_video, file_path, method, video_id)
        
        return {"video_id": video_id}

    except Exception as e:
        logger.exception(f"Error in video processing: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error in video processing: {str(e)}")

@app.get("/status/{video_id}")
async def get_status(video_id: str):
    if video_id not in processing_status:
        raise HTTPException(status_code=404, detail="Video ID not found")
    
    status = processing_status[video_id]
    
    if status['status'] == 'completed':
        output_path = status['output_path']
        if not os.path.exists(output_path):
            raise HTTPException(status_code=404, detail="Processed video file not found")
        
        return FileResponse(output_path, media_type="video/webm", filename=f"processed_video_{video_id}.webm")
    
    return status

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_old_videos())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9876)