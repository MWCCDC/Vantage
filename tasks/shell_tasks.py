# tasks/shell_tasks.py
import subprocess
import logging
from celery_app import celery

logger = logging.getLogger(__name__)

@celery.task(bind=True, max_retries=3, default_retry_delay=60)
def run_curl_command(self, url, method='GET', headers=None, data=None):
    """
    Executes a curl command via subprocess.
    """
    cmd = ['curl', '-s', '-X', method]
    
    if headers:
        for key, value in headers.items():
            cmd.extend(['-H', f'{key}: {value}'])
    
    if data:
        cmd.extend(['-d', data])
    
    cmd.append(url)
    
    logger.info(f"Executing: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            raise Exception(f"Command failed with code {result.returncode}: {result.stderr}")
            
        return {
            "status": "success",
            "stdout": result.stdout,
            "stderr": result.stderr
        }
        
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out, retrying...")
        raise self.retry(exc=Exception("Timeout"))
    except Exception as e:
        logger.error(f"Command execution failed: {e}")
        raise self.retry(exc=e)