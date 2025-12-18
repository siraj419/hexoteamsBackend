"""
S3 Service Module
This module provides a comprehensive S3 service class for performing all S3 operations.
"""
import os
import io
import mimetypes
from typing import Optional, List, Dict, Any, BinaryIO
from datetime import datetime
import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError, NoCredentialsError
import logging

from app.core import settings

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class S3ServiceException(Exception):
    """Base exception for S3 service errors"""
    pass


class S3Service:
    """
    S3 Service class for performing all S3 operations.
    
    This service provides methods for:
    - File upload (single and multipart)
    - File download
    - File deletion
    - List files/objects
    - Generate presigned URLs
    - Bucket operations
    - Folder operations
    - File metadata operations
    """
    
    def __init__(self):
        """
        Initialize S3 Service.
        
        Args:
            config: S3Config instance. If None, uses default config from environment.
        """
        self.s3_client = None
        self.s3_resource = None
        self._initialized = False
        self._initializing = False
        # Don't initialize immediately - wait for first use
    
    def _initialize(self):
        """Initialize S3 client and resource, with retry logic for MinIO."""
        if self._initializing:
            return
        if self._initialized:
            return
        
        self._initializing = True
        import time
        max_retries = 10
        retry_delay = 3
        
        endpoint = settings.S3_ENDPOINT_URL or "AWS S3"
        logger.info(f"Initializing S3 service (endpoint: {endpoint}, bucket: {settings.S3_BUCKET_NAME})")
        
        try:
            for attempt in range(max_retries):
                try:
                    self.s3_client = self._create_s3_client()
                    self.s3_resource = self._create_s3_resource()
                    
                    # Test connection by listing buckets first (lighter operation)
                    try:
                        self.s3_client.list_buckets()
                        logger.debug("S3 connection test successful")
                    except Exception as conn_err:
                        logger.warning(f"S3 connection test failed: {conn_err}")
                        if attempt < max_retries - 1:
                            raise  # Retry
                        else:
                            raise
                    
                    # Ensure default bucket exists
                    self._ensure_bucket_exists(settings.S3_BUCKET_NAME)
                    self._initialized = True
                    self._initializing = False
                    logger.info(f"S3 service initialized successfully (endpoint: {endpoint})")
                    return
                except (ClientError, Exception) as e:
                    error_str = str(e)
                    error_code = None
                    if isinstance(e, ClientError):
                        error_code = e.response.get('Error', {}).get('Code', '')
                    
                    if attempt < max_retries - 1:
                        if error_code == '403' or 'Forbidden' in error_str:
                            logger.warning(f"MinIO access denied (attempt {attempt + 1}/{max_retries}). Check credentials. Retrying in {retry_delay}s...")
                        elif error_code in ('404', 'NoSuchBucket'):
                            logger.warning(f"Bucket not found (attempt {attempt + 1}/{max_retries}). Will create. Retrying in {retry_delay}s...")
                        else:
                            logger.warning(f"S3 initialization attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                else:
                    logger.error(f"S3 initialization failed after {max_retries} attempts: {e}")
                    logger.error(f"Endpoint: {endpoint}, Bucket: {settings.S3_BUCKET_NAME}, Access Key: {settings.AWS_ACCESS_KEY_ID[:4]}...")
                    logger.warning("S3 service will be initialized lazily on first use")
                    # Don't raise - allow lazy initialization
                    self._initializing = False
        except Exception as e:
            self._initializing = False
            logger.error(f"Unexpected error during S3 initialization: {e}")
            raise
    
    def _ensure_initialized(self):
        """Ensure S3 client is initialized before use."""
        if not self._initialized:
            self._initialize()
            if not self._initialized:
                raise S3ServiceException("S3 service failed to initialize. Check MinIO connection and credentials.")
    
    def _ensure_bucket_exists(self, bucket_name: str) -> None:
        """Check if bucket exists and create it if it doesn't."""
        try:
            self.s3_client.head_bucket(Bucket=bucket_name)
            logger.debug(f"Bucket '{bucket_name}' exists")
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            error_message = e.response.get('Error', {}).get('Message', '')
            
            if error_code in ('404', 'NoSuchBucket'):
                logger.info(f"Bucket '{bucket_name}' not found, creating...")
                try:
                    self.create_bucket(bucket_name)
                    logger.info(f"✅ Created bucket: {bucket_name}")
                except Exception as create_err:
                    logger.error(f"Failed to create bucket: {create_err}")
                    raise S3ServiceException(f"Bucket '{bucket_name}' does not exist and could not be created: {create_err}")
            elif error_code == '403' or 'Forbidden' in str(e):
                logger.warning(f"Access denied when checking bucket '{bucket_name}'. This may be normal if MinIO is still starting up.")
                raise
            else:
                logger.error(f"Error checking bucket: {e}")
                raise
    
    def _create_s3_client(self):
        """Create and return boto3 S3 client"""
        try:
            boto_config = BotoConfig(
                signature_version=settings.S3_SIGNATURE_VERSION,
                region_name=settings.AWS_REGION
            )
            
            is_minio = settings.S3_ENDPOINT_URL is not None and settings.S3_ENDPOINT_URL != ''
            if is_minio:
                boto_config = BotoConfig(
                    signature_version=settings.S3_SIGNATURE_VERSION,
                    s3={'addressing_style': 'path'}
                )
            
            client_params = {
                'aws_access_key_id': settings.AWS_ACCESS_KEY_ID,
                'aws_secret_access_key': settings.AWS_SECRET_ACCESS_KEY,
                'config': boto_config,
                'region_name': settings.AWS_REGION,
                'use_ssl': settings.S3_USE_SSL
            }
            
            if settings.S3_ENDPOINT_URL:
                client_params['endpoint_url'] = settings.S3_ENDPOINT_URL
            
            return boto3.client('s3', **client_params)
        except Exception as e:
            logger.error(f"Failed to create S3 client: {str(e)}")
            raise S3ServiceException(f"Failed to create S3 client: {str(e)}")
    
    def _create_s3_resource(self):
        """Create and return boto3 S3 resource"""
        try:
            is_minio = settings.S3_ENDPOINT_URL is not None and settings.S3_ENDPOINT_URL != ''
            
            resource_params = {
                'aws_access_key_id': settings.AWS_ACCESS_KEY_ID,
                'aws_secret_access_key': settings.AWS_SECRET_ACCESS_KEY,
                'region_name': settings.AWS_REGION,
                'use_ssl': settings.S3_USE_SSL
            }
            
            if settings.S3_ENDPOINT_URL:
                resource_params['endpoint_url'] = settings.S3_ENDPOINT_URL
            
            if is_minio:
                from botocore.client import Config
                resource_params['config'] = Config(signature_version=settings.S3_SIGNATURE_VERSION, s3={'addressing_style': 'path'})
            
            return boto3.resource('s3', **resource_params)
        except Exception as e:
            logger.error(f"Failed to create S3 resource: {str(e)}")
            raise S3ServiceException(f"Failed to create S3 resource: {str(e)}")
    
    # ==================== FILE UPLOAD OPERATIONS ====================
    
    def upload_file(
        self,
        file: BinaryIO,
        key: str,
        bucket_name: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        public_read: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Upload a file to S3.
        
        Args:
            file: File-like object to upload
            key: S3 object key (path in bucket)
            bucket_name: Bucket name (uses default if None)
            content_type: MIME type of file (auto-detected if None)
            metadata: Custom metadata dictionary
            public_read: Make file publicly readable (uses config default if None)
        
        Returns:
            Dict containing upload information (bucket, key, url, etc.)
        
        Raises:
            S3ServiceException: If upload fails
        """
        self._ensure_initialized()
        bucket = bucket_name or settings.S3_BUCKET_NAME
        public = public_read if public_read is not None else settings.S3_PUBLIC_READ
        
        try:
            # Detect content type if not provided
            if not content_type:
                content_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
            
            # Prepare extra args
            extra_args = {
                'ContentType': content_type
            }
            
            if metadata:
                extra_args['Metadata'] = metadata
            
            if public:
                extra_args['ACL'] = 'public-read'
            
            # Upload file
            self.s3_client.upload_fileobj(file, bucket, key, ExtraArgs=extra_args)
            
            # Get file URL
            url = self.get_file_url(key, bucket)
            
            logger.info(f"Successfully uploaded file to s3://{bucket}/{key}")
            
            return {
                'success': True,
                'bucket': bucket,
                'key': key,
                'url': url,
                'content_type': content_type,
                'public': public,
                'uploaded_at': datetime.utcnow().isoformat()
            }
        
        except ClientError as e:
            logger.error(f"Failed to upload file: {str(e)}")
            raise S3ServiceException(f"Failed to upload file: {str(e)}")
    
    def upload_file_from_path(
        self,
        file_path: str,
        key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Upload a file from local file system.
        
        Args:
            file_path: Local file path
            key: S3 object key (uses filename if None)
            bucket_name: Bucket name (uses default if None)
            **kwargs: Additional arguments passed to upload_file
        
        Returns:
            Dict containing upload information
        """
        if not os.path.exists(file_path):
            raise S3ServiceException(f"File not found: {file_path}")
        
        if not key:
            key = os.path.basename(file_path)
        
        with open(file_path, 'rb') as f:
            return self.upload_file(f, key, bucket_name, **kwargs)
    
    # ==================== FILE DOWNLOAD OPERATIONS ====================
    
    def download_file(
        self,
        key: str,
        bucket_name: Optional[str] = None
    ) -> bytes:
        """
        Download a file from S3 as bytes.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            File contents as bytes
        
        Raises:
            S3ServiceException: If download fails
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            response = self.s3_client.get_object(Bucket=bucket, Key=key)
            return response['Body'].read()
        except ClientError as e:
            logger.error(f"Failed to download file: {str(e)}")
            raise S3ServiceException(f"Failed to download file: {str(e)}")
    
    def download_file_to_path(
        self,
        key: str,
        local_path: str,
        bucket_name: Optional[str] = None
    ) -> str:
        """
        Download a file from S3 to local file system.
        
        Args:
            key: S3 object key
            local_path: Local file path to save to
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            Local file path
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            self.s3_client.download_file(bucket, key, local_path)
            logger.info(f"Successfully downloaded s3://{bucket}/{key} to {local_path}")
            return local_path
        except ClientError as e:
            logger.error(f"Failed to download file: {str(e)}")
            raise S3ServiceException(f"Failed to download file: {str(e)}")
    
    # ==================== FILE DELETE OPERATIONS ====================
    
    def delete_file(
        self,
        key: str,
        bucket_name: Optional[str] = None
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        """
        Delete a file from S3.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            Dict containing deletion information
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            self.s3_client.delete_object(Bucket=bucket, Key=key)
            logger.info(f"Successfully deleted s3://{bucket}/{key}")
            return {
                'success': True,
                'bucket': bucket,
                'key': key,
                'deleted_at': datetime.utcnow().isoformat()
            }
        except ClientError as e:
            logger.error(f"Failed to delete file: {str(e)}")
            raise S3ServiceException(f"Failed to delete file: {str(e)}")
    
    def delete_files(
        self,
        keys: List[str],
        bucket_name: Optional[str] = None
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        """
        Delete multiple files from S3.
        
        Args:
            keys: List of S3 object keys
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            Dict containing deletion information
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        # Filter out empty/None keys and strip whitespace
        valid_keys = [key.strip() for key in keys if key and key.strip()]
        
        # Handle empty keys list
        if not valid_keys:
            logger.warning("No valid keys provided for deletion")
            return {
                'success': True,
                'bucket': bucket,
                'deleted_count': 0,
                'deleted_keys': [],
                'errors': [],
                'deleted_at': datetime.utcnow().isoformat()
            }
        
        try:
            objects = [{'Key': key} for key in valid_keys]
            response = self.s3_client.delete_objects(
                Bucket=bucket,
                Delete={'Objects': objects}
            )
            
            deleted = response.get('Deleted', [])
            errors = response.get('Errors', [])
            
            if errors:
                logger.warning(f"Errors occurred during deletion: {errors}")
            
            logger.info(f"Successfully deleted {len(deleted)} files from s3://{bucket}")
            
            return {
                'success': True,
                'bucket': bucket,
                'deleted_count': len(deleted),
                'deleted_keys': [obj['Key'] for obj in deleted],
                'errors': errors,
                'deleted_at': datetime.utcnow().isoformat()
            }
        except ClientError as e:
            logger.error(f"Failed to delete files: {str(e)}")
            raise S3ServiceException(f"Failed to delete files: {str(e)}")
    
    # ==================== LIST OPERATIONS ====================
    
    def list_files(
        self,
        prefix: str = "",
        bucket_name: Optional[str] = None,
        max_keys: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        List files in S3 bucket.
        
        Args:
            prefix: Prefix to filter objects (folder path)
            bucket_name: Bucket name (uses default if None)
            max_keys: Maximum number of keys to return
        
        Returns:
            List of file information dictionaries
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            response = self.s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=prefix,
                MaxKeys=max_keys
            )
            
            if 'Contents' not in response:
                return []
            
            files = []
            for obj in response['Contents']:
                files.append({
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'last_modified': obj['LastModified'].isoformat(),
                    'etag': obj['ETag'].strip('"'),
                    'storage_class': obj.get('StorageClass', 'STANDARD')
                })
            
            return files
        except ClientError as e:
            logger.error(f"Failed to list files: {str(e)}")
            raise S3ServiceException(f"Failed to list files: {str(e)}")
    
    # ==================== PRESIGNED URL OPERATIONS ====================
    
    def generate_presigned_url(
        self,
        key: str,
        bucket_name: Optional[str] = None,
        expiration: Optional[int] = None,
        http_method: str = 'GET'
    ) -> str:
        self._ensure_initialized()
        """
        Generate a presigned URL for S3 object.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
            expiration: URL expiration in seconds (uses config default if None)
            http_method: HTTP method (GET, PUT, etc.)
        
        Returns:
            Presigned URL string
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        expiration = expiration or settings.S3_PRESIGNED_URL_EXPIRATION
        
        try:
            method_map = {
                'GET': 'get_object',
                'PUT': 'put_object',
                'DELETE': 'delete_object'
            }
            
            client_method = method_map.get(http_method.upper(), 'get_object')
            
            url = self.s3_client.generate_presigned_url(
                ClientMethod=client_method,
                Params={'Bucket': bucket, 'Key': key},
                ExpiresIn=expiration
            )
            
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL: {str(e)}")
            raise S3ServiceException(f"Failed to generate presigned URL: {str(e)}")
    
    def generate_presigned_post(
        self,
        key: str,
        bucket_name: Optional[str] = None,
        expiration: Optional[int] = None,
        max_size_mb: Optional[int] = None
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        """
        Generate a presigned POST for direct browser upload.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
            expiration: URL expiration in seconds
            max_size_mb: Maximum file size in MB
        
        Returns:
            Dict containing presigned POST data (url and fields)
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        expiration = expiration or settings.S3_PRESIGNED_URL_EXPIRATION
        max_size = (max_size_mb or settings.S3_MAX_FILE_SIZE_MB) * 1024 * 1024
        
        try:
            conditions = [
                ['content-length-range', 0, max_size]
            ]
            
            response = self.s3_client.generate_presigned_post(
                Bucket=bucket,
                Key=key,
                ExpiresIn=expiration,
                Conditions=conditions
            )
            
            return response
        except ClientError as e:
            logger.error(f"Failed to generate presigned POST: {str(e)}")
            raise S3ServiceException(f"Failed to generate presigned POST: {str(e)}")
    
    # ==================== FILE METADATA OPERATIONS ====================
    
    def get_file_metadata(
        self,
        key: str,
        bucket_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get metadata for a file in S3.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            Dict containing file metadata
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            response = self.s3_client.head_object(Bucket=bucket, Key=key)
            
            return {
                'key': key,
                'size': response.get('ContentLength'),
                'content_type': response.get('ContentType'),
                'last_modified': response.get('LastModified').isoformat() if response.get('LastModified') else None,
                'etag': response.get('ETag', '').strip('"'),
                'metadata': response.get('Metadata', {}),
                'storage_class': response.get('StorageClass', 'STANDARD'),
                'version_id': response.get('VersionId')
            }
        except ClientError as e:
            logger.error(f"Failed to get file metadata: {str(e)}")
            raise S3ServiceException(f"Failed to get file metadata: {str(e)}")
    
    def file_exists(
        self,
        key: str,
        bucket_name: Optional[str] = None
    ) -> bool:
        """
        Check if a file exists in S3.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            True if file exists, False otherwise
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        try:
            self.s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            raise S3ServiceException(f"Error checking file existence: {str(e)}")
    
    # ==================== COPY/MOVE OPERATIONS ====================
    
    def copy_file(
        self,
        source_key: str,
        destination_key: str,
        source_bucket: Optional[str] = None,
        destination_bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Copy a file within S3.
        
        Args:
            source_key: Source S3 object key
            destination_key: Destination S3 object key
            source_bucket: Source bucket name (uses default if None)
            destination_bucket: Destination bucket name (uses default if None)
        
        Returns:
            Dict containing copy information
        """
        src_bucket = source_bucket or settings.S3_BUCKET_NAME
        dst_bucket = destination_bucket or settings.S3_BUCKET_NAME
        
        try:
            copy_source = {'Bucket': src_bucket, 'Key': source_key}
            self.s3_client.copy_object(
                CopySource=copy_source,
                Bucket=dst_bucket,
                Key=destination_key
            )
            
            logger.info(f"Successfully copied s3://{src_bucket}/{source_key} to s3://{dst_bucket}/{destination_key}")
            
            return {
                'success': True,
                'source_bucket': src_bucket,
                'source_key': source_key,
                'destination_bucket': dst_bucket,
                'destination_key': destination_key,
                'copied_at': datetime.utcnow().isoformat()
            }
        except ClientError as e:
            logger.error(f"Failed to copy file: {str(e)}")
            raise S3ServiceException(f"Failed to copy file: {str(e)}")
    
    def move_file(
        self,
        source_key: str,
        destination_key: str,
        source_bucket: Optional[str] = None,
        destination_bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Move a file within S3 (copy + delete).
        
        Args:
            source_key: Source S3 object key
            destination_key: Destination S3 object key
            source_bucket: Source bucket name (uses default if None)
            destination_bucket: Destination bucket name (uses default if None)
        
        Returns:
            Dict containing move information
        """
        # Copy file
        copy_result = self.copy_file(source_key, destination_key, source_bucket, destination_bucket)
        
        # Delete source file
        src_bucket = source_bucket or settings.S3_BUCKET_NAME
        self.delete_file(source_key, src_bucket)
        
        return {
            'success': True,
            'source_bucket': copy_result['source_bucket'],
            'source_key': source_key,
            'destination_bucket': copy_result['destination_bucket'],
            'destination_key': destination_key,
            'moved_at': datetime.utcnow().isoformat()
        }
    
    # ==================== BUCKET OPERATIONS ====================
    
    def create_bucket(
        self,
        bucket_name: str,
        region: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new S3 bucket.
        
        Args:
            bucket_name: Name of bucket to create
            region: AWS region (uses config default if None)
        
        Returns:
            Dict containing bucket creation information
        """
        if not self._initializing and not self._initialized:
            self._ensure_initialized()
        elif not self.s3_client:
            raise S3ServiceException("S3 client not available")
        
        region = region or settings.AWS_REGION
        
        try:
            is_minio = settings.S3_ENDPOINT_URL is not None and settings.S3_ENDPOINT_URL != ''
            if is_minio:
                self.s3_client.create_bucket(Bucket=bucket_name)
            elif region == 'us-east-1':
                self.s3_client.create_bucket(Bucket=bucket_name)
            else:
                self.s3_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={'LocationConstraint': region}
                )
            
            logger.info(f"Successfully created bucket: {bucket_name}")
            
            return {
                'success': True,
                'bucket': bucket_name,
                'region': region,
                'created_at': datetime.utcnow().isoformat()
            }
        except ClientError as e:
            logger.error(f"Failed to create bucket: {str(e)}")
            raise S3ServiceException(f"Failed to create bucket: {str(e)}")
    
    def list_buckets(self) -> List[Dict[str, Any]]:
        """
        List all S3 buckets.
        
        Returns:
            List of bucket information dictionaries
        """
        try:
            response = self.s3_client.list_buckets()
            
            buckets = []
            for bucket in response.get('Buckets', []):
                buckets.append({
                    'name': bucket['Name'],
                    'creation_date': bucket['CreationDate'].isoformat()
                })
            
            return buckets
        except ClientError as e:
            logger.error(f"Failed to list buckets: {str(e)}")
            raise S3ServiceException(f"Failed to list buckets: {str(e)}")
    
    def delete_bucket(
        self,
        bucket_name: str,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Delete an S3 bucket.
        
        Args:
            bucket_name: Name of bucket to delete
            force: If True, delete all objects in bucket first
        
        Returns:
            Dict containing deletion information
        """
        try:
            if force:
                # Delete all objects first
                bucket = self.s3_resource.Bucket(bucket_name)
                bucket.objects.all().delete()
            
            self.s3_client.delete_bucket(Bucket=bucket_name)
            logger.info(f"Successfully deleted bucket: {bucket_name}")
            
            return {
                'success': True,
                'bucket': bucket_name,
                'deleted_at': datetime.utcnow().isoformat()
            }
        except ClientError as e:
            logger.error(f"Failed to delete bucket: {str(e)}")
            raise S3ServiceException(f"Failed to delete bucket: {str(e)}")
    
    # ==================== UTILITY METHODS ====================
    
    def get_file_url(
        self,
        key: str,
        bucket_name: Optional[str] = None
    ) -> str:
        self._ensure_initialized()
        """
        Get the public URL of a file.
        
        Args:
            key: S3 object key
            bucket_name: Bucket name (uses default if None)
        
        Returns:
            File URL string
        """
        bucket = bucket_name or settings.S3_BUCKET_NAME
        
        if settings.S3_ENDPOINT_URL:
            return f"{settings.S3_ENDPOINT_URL}/{bucket}/{key}"
        else:
            return f"https://{bucket}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
    
    def validate_file_extension(self, filename: str) -> bool:
        """
        Validate file extension against allowed extensions.
        Note: This method doesn't require S3 initialization.
        
        Args:
            filename: File name to validate
        
        Returns:
            True if extension is allowed, False otherwise
        """
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return ext in settings.S3_ALLOWED_EXTENSIONS_LIST
    
    def validate_file_size(self, size_bytes: int) -> bool:
        """
        Validate file size against maximum allowed size.
        Note: This method doesn't require S3 initialization.
        
        Args:
            size_bytes: File size in bytes
        
        Returns:
            True if size is within limit, False otherwise
        """
        return size_bytes <= settings.S3_MAX_FILE_SIZE_MB * 1024 * 1024

class _S3ServiceProxy:
    """Proxy for lazy S3 service initialization."""
    def __init__(self):
        self._instance = None
    
    def __getattr__(self, name):
        if self._instance is None:
            self._instance = S3Service()
        return getattr(self._instance, name)

s3_service = _S3ServiceProxy()