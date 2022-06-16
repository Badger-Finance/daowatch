#!/bin/bash
### this script requires that AWS_REGION is set and that proper AWS credentials are configured in environment variables
### the credentials should have admin access to ecr, or be those of the scout-deploy user provided by terraform
### and found in SSM parameter store
### ECR URL must be set to the root hostname of your account/region ecr - see example
ECR_URL=342684350154.dkr.ecr.us-west-1.amazonaws.com
time_tag=`date '+%Y%m%d%H%M%S'`
for app in grafana prometheus scout
do
  aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
  docker buildx build --platform linux/amd64,linux/arm64 -t $ECR_URL/daowatch_$app:$time_tag -t $ECR_URL/daowatch_$app:latest ./$app
  docker push  $ECR_URL/daowatch_$app:$time_tag
  docker push  $ECR_URL/daowatch_$app:latest
done
