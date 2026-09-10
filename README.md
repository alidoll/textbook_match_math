# textbook-match

教材匹配与旧库建设 Web 系统（Flask + MySQL）。

## 快速开始

1. 安装 **Python 3.12**（见 `docs/github-collab.md`）
2. 复制 `.env.example` → `.env`，填写 MySQL 连接
3. 安装依赖并启动：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\init_db.py    # 仅首次 / 新库需要
python run.py
```

浏览器：http://127.0.0.1:5000/

## 文档

| 文档 | 说明 |
|------|------|
| [docs/git-同事上手.md](docs/git-同事上手.md) | **同事零基础：改代码怎么 push** |
| [docs/github-collab.md](docs/github-collab.md) | GitHub 建库、邀请协作者、双人协作 |
| [docs/mysql-remote-connect.md](docs/mysql-remote-connect.md) | 远程连 MySQL、DBeaver、本地 `.env` |
| [docs/conda-setup.md](docs/conda-setup.md) | Conda 环境（可选） |
| [table.md](table.md) | 数据库表设计 |

## 协作约定（摘要）

- **代码**：GitHub 同步（`git pull` / `push`）
- **数据库**：共用一台 MySQL（负责人主机），**不进 Git**
- **`.env`**：每人本地一份，**不提交**
- **大文件**（PDF、xlsx、课件图）：见 `.gitignore`，网盘或负责人机器共享
