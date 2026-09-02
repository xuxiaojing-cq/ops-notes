# ops-notes

运维学习笔记与实践记录。记录日常排查方法、监控告警配置、容器与 Kubernetes 实践，以及自动化相关内容。

## 背景

本人从事 To B 交付实施与线上运维工作，负责医疗影像业务在数百家机构的部署交付与运维保障，日常处理线上故障排查、监控告警配置、批量部署与数据核查。

本仓库用于系统整理工作中的排查方法，并补齐监控体系、容器编排、CI/CD 等方向的知识与实操记录。

## 目录

| 目录 | 内容 |
|---|---|
| [01-linux](./01-linux) | Linux 故障排查方法论、性能分析工具、常见问题定位 |
| [02-monitoring](./02-monitoring) | Prometheus / Grafana / Alertmanager 配置与告警设计 |
| [03-container](./03-container) | Docker 镜像构建、Compose 编排、容器排障 |
| [04-k8s](./04-k8s) | Kubernetes 核心对象、yaml 实践、排障 checklist |
| [05-cicd](./05-cicd) | GitHub Actions、Ansible、发布与回滚 |
| [06-interview](./06-interview) | 通用技术问题整理 |
| [99-assets](./99-assets) | 图片与截图 |

## 进度

- [ ] Git 基础与分支协作
- [ ] Linux 故障排查方法论（USE 方法 / 60 秒排查法）
- [ ] SRE 核心理念（SLI / SLO / 告警设计 / 复盘）
- [ ] Prometheus + node_exporter + Grafana 搭建
- [ ] PromQL 常用表达式
- [ ] Alertmanager 告警规则与路由
- [ ] Dockerfile 编写与多阶段构建
- [ ] Kubernetes 核心对象与排障
- [ ] GitHub Actions 流水线
- [ ] Ansible playbook 改写部署脚本

## 说明

内容以自学与实操记录为主，涉及工作场景的部分均已做脱敏处理，不包含任何客户信息、业务数据与内部系统细节。

