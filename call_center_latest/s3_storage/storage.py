import requests, os
import boto3
from botocore.client import Config
from dotenv import load_dotenv

from db.queries import get_all_path

load_dotenv()

BUCKET = os.getenv("S3_BUCKET_NAME", "call-center")

s3 = boto3.client(
            "s3",
                endpoint_url=os.getenv("S3_STORAGE_URL","http://172.28.23.101:3900"),
                    aws_access_key_id=os.getenv("S3_KEY_ID", "GK6d3c9bdaf79f8317c69119bf"),
                        aws_secret_access_key=os.getenv("S3_KEY", "9ee9b03bca7d76ae5bf2945180558d72c2be0231ede5f050fcc73ae97111030a"),
                            config=Config(signature_version="s3v4"),
                                region_name=os.getenv("S3_GARAGE", "garage"),
                                )


async def put_object(key, body):
    r = s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    status_code = r["ResponseMetadata"]["HTTPStatusCode"]
    print(f"{key} put to s3. Status: {status_code}")
    if status_code == 200:
        return True
    else:
        return False


async def get_object(key):
    data = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    print(f"Downloaded: {data[:20]}... ")
    return data



# if __name__ == "__main__":
#     import asyncio
#     a = False
#     paths = get_all_path()
#     for p in paths:
#         try:
#             p = p[0]
   
#             print(p)
#             with open("/var/spool/asterisk/monitor/" + p, "rb") as f:
#                 data = f.read()
#             asyncio.run(put_object(p, data))
#         except Exception as e:
#             print(f"Error uploading {p}: {e}")
#     # print(len(paths))
#     # asyncio.run(get_object("recordings/session_9695/turn016_ai.wav"))