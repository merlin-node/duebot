# DueBot

在 Telegram 里管理各种付费订阅的到期提醒：添加、修改、删除都在 bot 面板里点按钮完成，到期前按设定天数每天定时推送。

- 不需要域名，不开任何端口，服务器只主动连接 Telegram
- 到期后按续费周期自动顺延
- 支持多人使用，每个人的订阅和提醒设置互相独立
- 镜像由 GitHub 自动构建，支持 amd64 / arm64，服务器只拉取不构建
- 单文件 + SQLite，内存占用约 30MB
- 只有被添加的账号能使用，陌生人发什么都不回应

## 准备两样东西

**Bot Token**：在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示起名，最后它会给你一串 `123456789:AAxxxx…`，这就是 Token。

**User ID**：找 [@userinfobot](https://t.me/userinfobot) 随便发一句话，它回复的 `Id` 那串数字就是你的 User ID。

## 安装

用 root 登录服务器，把下面的命令依次复制、粘贴、回车。

**方式一：从 GitHub 安装**

```bash
apt update && apt install -y git
git clone https://github.com/merlin-node/duebot.git
cd duebot
bash install.sh
```

**方式二：上传 zip 安装**

先把 `duebot.zip` 上传到服务器的 `/root` 目录，然后：

```bash
apt update && apt install -y unzip
cd /root && unzip duebot.zip
cd duebot
bash install.sh
```

> CentOS / Rocky 等系统把 `apt` 换成 `yum`。

脚本会依次问你：

1. **Bot Token**：粘贴，回车
2. **User ID**：粘贴，回车
3. **代理地址**：海外服务器直接回车跳过

没装 Docker 的话脚本会自动安装。看到 `DueBot 已启动` 就完成了，去 Telegram 给你的 bot 发送 `/menu`。

## 使用

发送 `/menu` 打开控制面板：

- **📋 订阅列表**：点进任意订阅可修改名称、到期日、金额、单位、周期，或续费一期、删除
- **➕ 添加订阅**：按提示依次填写名称、周期、到期日、金额、单位
- **⏰ 提醒设置**：选择提前几天提醒（默认 7 / 3 / 1 天）和每天推送时间（默认 09:00）
- **🔔 预览提醒**：查看今天的通知会是什么样子
- **📤 导出备份**：把全部数据导出成 JSON 文件

管理员的面板里还会多一个 **👥 用户管理**，见下一节。

到期日支持 `2026-12-31`、`2026/12/31`、`20261231`、`12-31`、`12月31日` 等写法。周期订阅填了已经过去的日期，会自动推到下一期；一次性订阅只写月日且已过，会算作明年。

## 给朋友使用

1. 让朋友找 [@userinfobot](https://t.me/userinfobot) 拿到 TA 的 User ID
2. 你在面板里点 **👥 用户管理 → ➕ 添加用户**，输入朋友的 ID
3. 朋友在 Telegram 里搜索你的 bot，发送 `/start` 就能用了

说明：

- 每个人只能看到、修改自己的订阅，提醒天数和推送时间也各自设置
- 管理员在用户管理里只能看到每个人有几个订阅，看不到具体内容
- 移除用户会把 TA 的订阅数据一起删除
- 每人最多 100 个订阅

## 常用命令

以下命令都在 `duebot` 目录里执行：

```bash
docker logs -f duebot          # 查看日志，Ctrl+C 退出
docker compose restart         # 重启
docker compose down            # 停止
docker compose up -d           # 改完 .env 后重新启动
```

**更新：**

```bash
docker compose pull && docker compose up -d
```

代码推送到 GitHub 后会自动构建新镜像（几分钟），构建完在服务器上执行这一行就是最新版。

**修改 Token 或管理员 ID：** 重新运行 `bash install.sh`，问是否沿用配置时输入 `n`。

**备份：** 所有数据都在 `data/subs.db` 这一个文件里，复制走就行；迁移到新服务器时，装好后把它放回 `data/` 目录再重启。

**卸载：**

```bash
docker compose down && cd .. && rm -rf duebot
```

## 镜像构建

每次推送到 `main` 分支，GitHub Actions 会自动构建镜像并发布到 `ghcr.io/merlin-node/duebot:latest`，进度在仓库的 **Actions** 页面查看。

**第一次构建完成后，需要把镜像设为公开**，否则服务器拉取不到：

仓库主页右侧 **Packages** → 点 **duebot** → **Package settings** → 最下面 **Change visibility** → 选 **Public**

## 国内服务器

国内连不上 Telegram，需要代理：

- 安装时在「代理地址」一步填写，例如 `socks5://172.17.0.1:7890` 或 `http://172.17.0.1:7890`
- 地址要填 `172.17.0.1`（Docker 访问宿主机的地址），不能填 `127.0.0.1`
- 宿主机上的代理软件需要开启「允许局域网连接」

如果 Docker 自动安装失败，先手动用国内镜像安装，再运行 `bash install.sh`：

```bash
curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun
```
