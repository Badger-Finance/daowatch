resource "aws_ecr_repository" "prometheus" {
  count = var.create_ecr == true ? 1 : 0
  name = "${var.app_name}_prometheus"
}
resource "aws_ecr_repository" "scout" {
  count = var.create_ecr == true ? 1 : 0
  name = "${var.app_name}_scout"
}
resource "aws_ecr_repository" "grafana" {
  count = var.create_ecr == true ? 1 : 0
  name = "${var.app_name}_grafana"
}
locals {
  all_repo_arns = var.create_ecr ? [aws_ecr_repository.grafana[0].arn, aws_ecr_repository.scout[0].arn, aws_ecr_repository.prometheus[0].arn] : []
}
data "aws_iam_policy_document" "scout-reader" {
  statement {
    sid = "AllAcountsRead"
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:DescribeRepositories"]
    resources = [for repoarn in local.all_repo_arns: "${repoarn}/*"]
  }
}
data "aws_iam_policy_document" "scout-deployer" {
  statement {
    sid = "DoDockerLogin"
    actions = [
      "ecr:GetAuthorizationToken"]
    resources = [
      "*"]
  }
  statement {
    sid = "PushPullECR"
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeRepositories"]
      resources = [for repoarn in local.all_repo_arns: "${repoarn}*"]
  }
}
resource "aws_iam_policy" "scout-deployer" {
  count = var.create_ecr == true ? 1 :0
  name_prefix = "scout-deployer"
  policy = data.aws_iam_policy_document.scout-deployer.json
  lifecycle {
    create_before_destroy = true
  }
}
resource "aws_iam_policy" "scout-reader" {
  count = var.create_ecr == true ? 1 :0
  name_prefix = "scout-reader"
  policy = data.aws_iam_policy_document.scout-reader.json
  lifecycle {
    create_before_destroy = true
  }
}
