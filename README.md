# DueBot

Telegram 订阅到期提醒 bot。添加、修改、删除订阅都在 bot 面板里点按钮完成，到期前按你设定的天数定时推送。

- 不需要域名，不开端口
- 到期后按续费周期自动顺延
- 支持多人使用，每个人的数据互相独立
- 只有被添加的账号能使用

## 准备

- **Bot Token**：找 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示创建，最后会得到一串 `123456789:AAxxxx…`
- **User ID**：找 [@userinfobot](https://t.me/userinfobot) 随便发一句话，回复里的 `Id` 就是

## 安装

用 root 登录服务器，复制粘贴：

```bash
apt update && apt install -y git
git clone https://github.com/merlin-node/duebot.git
cd duebot
bash install.sh
```

按提示粘贴 Bot Token 和 User ID。没装 Docker 会自动安装。看到 `DueBot 已启动` 后，给你的 bot 发送 `/menu`。

## 使用

发送 `/menu` 打开面板：

- **📋 订阅列表**：查看、修改、续费、删除
- **➕ 添加订阅**：依次填写名称、周期、到期日、金额、单位
- **⏰ 提醒设置**：提前几天提醒（默认 7 / 3 / 1 天）、每天推送时间（默认 09:00）
- **🔔 预览提醒**：查看今天的通知
- **📤 导出备份 / 📥 导入备份**：导出 JSON 文件；导入时可选替换全部或追加

## 添加用户

1. 对方找 [@userinfobot](https://t.me/userinfobot) 拿到 User ID
2. 在面板里点 **👥 用户管理 → ➕ 添加用户**，输入 TA 的 ID
3. 对方给 bot 发送 `/start`

每个人只能看到自己的订阅。管理员只能看到每人的订阅数量。

## 常用命令

在 `duebot` 目录里执行：

```bash
docker compose pull && docker compose up -d   # 更新
docker compose restart                        # 重启
docker compose down                           # 停止
docker logs -f duebot                         # 查看日志
```

修改 Token 或 User ID：重新运行 `bash install.sh`，问是否沿用配置时输入 `n`。

数据都在 `data/subs.db`，备份复制这个文件就行。
