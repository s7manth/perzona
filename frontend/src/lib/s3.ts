import AWS from "aws-sdk";
import { env } from "~/env";

export async function getPresignedUrl(key: string) {
  const s3 = new AWS.S3({
    region: env.AWS_REGION,
    credentials: {
      accessKeyId: env.AWS_ACCESS_KEY_ID,
      secretAccessKey: env.AWS_SECRET_ACCESS_KEY_ID,
    },
  });

  return s3.getSignedUrl("getObject", {
    Bucket: env.S3_BUCKET_NAME,
    Key: key,
    Expires: 3600, // 1 hour
  });
}
