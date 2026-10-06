# 苦糖果 MES 启动说明

本文根据当前目录中的后端、前端、配置文件和 SQL 脚本整理。项目采用前后端分离结构：

- 后端：Spring Boot 2.5.12、Maven 多模块工程，默认端口 `8080`
- 前端：Vue 2.6 + Vue CLI 4，开发服务器默认端口 `80`
- 基础服务：MySQL、Redis
- 可选服务：MinIO，仅使用 MinIO 上传功能时需要

## 1. 目录说明

从工作区根目录 `E:\ktg_agent\ktg_agent_v1` 开始：

```text
ktg-mes-master/ktg-mes-master/   后端工程、SQL 和项目资料
ktg-mes-ui/                      前端工程
```

后端启动类是：

```text
ktg-mes-master/ktg-mes-master/ktg-admin/src/main/java/com/ktg/RuoYiApplication.java
```

## 2. 环境要求

建议使用以下环境：

| 软件 | 建议版本 | 说明 |
| --- | --- | --- |
| JDK | **JDK 8** | 必须优先使用 JDK 8；源码引用了 JDK 8 自带的 CORBA 类，高版本 JDK 会编译失败 |
| Maven | 3.6 或更高 | 用于构建后端 |
| MySQL | 5.7 或 8.0 | 数据库脚本使用 MySQL 语法 |
| Redis | 7.4（Docker） | 当前使用官方 `redis:7.4.11-alpine` 镜像，默认地址为 `localhost:6379`，无密码 |
| Node.js | 14 LTS 或 16 LTS | 这是 Vue CLI 4 老项目，不建议直接使用 Node 18/20/22 |
| npm | 随 Node.js 安装 | 用于安装和运行前端依赖 |

检查环境：

```powershell
java -version
mvn -version
node --version
npm --version
```

`java -version` 和 `mvn -version` 中显示的 Java 都应为 1.8。只修改 `JAVA_HOME` 后如果 Maven 仍显示其他版本，请重新打开终端再检查。

本工作区已经包含一套局部 JDK 8，不需要修改系统 Java。打开 PowerShell，执行：

```powershell
cd E:\ktg_agent\ktg_agent_v1
. .\use-java8.ps1
mvn -version
```

第一行开头的 `. ` 表示在当前 PowerShell 窗口中启用环境。关闭窗口后设置自动失效；系统原有 Java 版本不会改变。局部 JDK 位于 `.runtime/java8`。

### 2.1 本地密码本

真实的本地开发凭据统一保存在工作区根目录的 `.env` 中，包括 MySQL、Redis、应用令牌、Druid、MinIO 和系统管理员密码。该文件已加入 `.gitignore`，不要提交、截图或发送给他人。

需要启动后端时，先在 PowerShell 中载入它：

```powershell
Set-Location E:\ktg_agent\ktg_agent_v1
. .\use-local-secrets.ps1
```

脚本只显示载入项数量，不会打印密码。`.env.example` 是不含真实密码的配置模板，可以进入版本控制。

## 3. 首次初始化数据库

### 3.1 创建数据库

先在 MySQL 中创建数据库。后端当前默认连接的库名是 `j2eedb`：

```sql
CREATE DATABASE j2eedb
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;
```

### 3.2 导入 SQL

选中 `j2eedb` 数据库，按下面顺序导入。可以使用 Navicat、DataGrip、MySQL Workbench 或 MySQL 命令行。

1. 基础系统表：
   - `ktg-mes-master/ktg-mes-master/sql/ry_20210908.sql`
   - `ktg-mes-master/ktg-mes-master/sql/quartz.sql`
2. MES 业务表：
   - `doc/设计文档/数据库设计/mes_sys.sql`
   - `doc/设计文档/数据库设计/mes-cal.sql`
   - `doc/设计文档/数据库设计/mes-dv.sql`
   - `doc/设计文档/数据库设计/mes-md.sql`
   - `doc/设计文档/数据库设计/mes-pro.sql`
   - `doc/设计文档/数据库设计/mes-qc.sql`
   - `doc/设计文档/数据库设计/mes-tm.sql`
   - `doc/设计文档/数据库设计/mes-wm.sql`
   - `doc/设计文档/数据库设计/ureport_file_tbl.sql`
3. 项目初始化数据：
   - `doc/实施文档/sys_menu.sql`
   - `doc/实施文档/sys_dict_type.sql`
   - `doc/实施文档/sys_dict_data.sql`
   - `doc/实施文档/sys_auto_code.sql`
4. 执行下方“新数据库基础数据”SQL。

以上 `doc/...` 路径都相对于后端目录 `ktg-mes-master/ktg-mes-master`。

> 注意：`sys_auto_code.sql` 已经包含编码规则、编码组成和编码结果三张表，不要再重复导入同目录中的 `sys_auto_code_rule.sql`、`sys_auto_code_part.sql` 和 `sys_auto_code_result.sql`。初始化脚本含有 `DROP TABLE`，不要在已有业务数据的数据库上重复执行。

原项目的 `doc/实施文档/inital.sql` 把新建库区和库位的父级 ID 固定写成了 `200`。在全新数据库中，自增 ID 通常不是 200，会造成仓库层级关联错误。因此全新安装时不要直接导入该文件，请执行下面的等价修正版：

```sql
INSERT INTO md_item_type
  (item_type_code, item_type_name, parent_type_id, ancestors, item_or_product, order_num, enable_flag)
VALUES
  ('ITEM_TYPE_0000', '物料产品分类', 0, '0', 'PRODUCT', 1, 'Y');

INSERT INTO dv_machinery_type
  (machinery_type_code, machinery_type_name, parent_type_id, ancestors, enable_flag)
VALUES
  ('M_TYPE_000', '设备分类', 0, '0', 'Y');

INSERT INTO wm_warehouse
  (warehouse_code, warehouse_name, location, area, charge)
VALUES
  ('XBK_VIRTUAL', '线边库-虚拟', NULL, -1, NULL);
SET @warehouse_id = LAST_INSERT_ID();

INSERT INTO wm_storage_location
  (location_code, location_name, warehouse_id, area, area_flag)
VALUES
  ('XBKKQ_VIRTUAL', '线边库库区-虚拟', @warehouse_id, -1, 'Y');
SET @location_id = LAST_INSERT_ID();

INSERT INTO wm_storage_area
  (area_code, area_name, location_id, area, enable_flag)
VALUES
  ('XBKKW_VIRTUAL', '线边库库位-虚拟', @location_id, -1, 'Y');
```

使用 MySQL 命令行时，可以先执行：

```sql
USE j2eedb;
SOURCE E:/ktg_agent/ktg_agent_v1/ktg-mes-master/ktg-mes-master/sql/ry_20210908.sql;
```

其余文件按上述顺序继续执行 `SOURCE`。路径建议使用 `/`，含中文路径时请确保客户端采用 UTF-8。

## 4. 配置后端

### 4.1 数据库连接

`application-druid.yml` 已改为从环境变量读取数据库连接和 Druid 控制台凭据。后端使用最小权限账号 `ktg_app`，不使用 MySQL root 账号。启动后端前执行 `. .\use-local-secrets.ps1` 即可，无需把密码写进 YAML。

### 4.2 Redis 和上传目录

`application.yml` 已改为从环境变量读取 Redis 密码、Token 密钥、上传目录和 MinIO 凭据。当前上传目录由 `.env` 中的 `KTG_UPLOAD_DIR` 指定。

普通登录和大部分页面不依赖 MinIO；使用条码图片等 MinIO 上传功能前，还需要启动 MinIO 并创建 `.env` 中 `MINIO_BUCKET` 指定的存储桶。

## 5. 启动基础服务

工作区根目录已经提供 `docker-compose.mysql.yml`，其中同时包含 MySQL 和 Redis。执行：

```powershell
Set-Location E:\ktg_agent\ktg_agent_v1
docker compose -f docker-compose.mysql.yml up -d
```

数据通过目录挂载保存在项目外部数据目录中，即使容器删除后也不会随容器丢失：

- MySQL：`E:\ktg_agent\ktg_agent_v1\mysql` → `/var/lib/mysql`
- Redis：`E:\ktg_agent\ktg_agent_v1\redis` → `/data`

两个服务都只监听本机地址：

- MySQL 能连接到 `localhost:3306`，且 `j2eedb` 中已有初始化表
- Redis 能连接到 `localhost:6379`

Redis 已开启 AOF 持久化和密码认证，密码由 Docker 与后端共同从 `.env` 读取。查看服务状态：

```powershell
docker compose -f docker-compose.mysql.yml ps
```

停止两个基础服务：

```powershell
docker compose -f docker-compose.mysql.yml stop
```

## 6. 启动后端

### 6.1 一键启动前后端（推荐）

首次使用 Agent 前，必须先在根目录 `.env` 中填写真实的 `MODEL_API_KEY` 和 `MODEL_ID`。保留空值或 `CHANGE_ME` 时，一键启动会在创建任何服务窗口前明确失败。

双击工作区根目录中的 `启动前后端.cmd`。脚本会：

1. 检查 Docker Desktop；
2. 启动并等待 MySQL、Redis 健康；
3. 检查后端 `8080` 和前端 `81` 端口；
4. 打开一个后端 PowerShell 窗口，自动加载 JDK 8、密码本、构建并启动后端；
5. 打开前端 PowerShell 窗口，使用项目 Node 16 启动前端；
6. 打开 Agent PowerShell 窗口，加载 `.env` 并启动真实模型查询服务；
7. 等待三个端口实际监听，并调用 Agent `/health`；只有全部通过才报告启动成功。

不要关闭这三个运行窗口。停止服务时，在对应窗口按 `Ctrl+C`。

### 6.2 手动启动后端

打开一个 PowerShell 窗口：

```powershell
Set-Location E:\ktg_agent\ktg_agent_v1
. .\use-java8.ps1
. .\use-local-secrets.ps1
Set-Location .\ktg-mes-master\ktg-mes-master
mvn -s ..\..\maven-settings.xml clean package -DskipTests
java -jar .\ktg-admin\target\ktg-admin.jar
```

也可以在 IntelliJ IDEA 中导入后端根目录的 `pom.xml`，选择 JDK 8，然后直接运行 `com.ktg.RuoYiApplication`。

看到 `KTM-MES启动成功` 表示 Spring Boot 已启动。后端默认地址：

```text
http://localhost:8080
```

辅助页面：

- Swagger：`http://localhost:8080/swagger-ui/index.html`
- Druid：`http://localhost:8080/druid/`
- UReport：`http://localhost:8080/ureport/designer`

如果只是后续再次启动，已有 JAR 未发生代码变化时可直接执行 `java -jar`；修改后端代码后应重新打包。

## 7. 启动前端

再打开一个 PowerShell 窗口：

```powershell
Set-Location E:\ktg_agent\ktg_agent_v1
. .\use-node16.ps1
Set-Location .\ktg-mes-ui
npm install --legacy-peer-deps --registry=https://registry.npmmirror.com
npm run dev -- --port 81
```

然后访问：

```text
http://localhost:81
```

项目原配置默认使用端口 `80`。这里使用 `81` 是为了避免 Windows 上端口 80 被占用或需要管理员权限；如果希望保持原端口，可运行 `npm run dev` 并访问 `http://localhost`。

开发环境下，前端会把 `/dev-api` 和 `/ureport` 请求代理到 `http://localhost:8080`，因此正常情况下不需要额外处理跨域。

系统管理员的默认密码已经轮换，不再是 `123456`。用户名和新密码请查看根目录 `.env` 中的 `APP_ADMIN_USERNAME` 与 `APP_ADMIN_PASSWORD`。验证码和登录令牌依赖 Redis，Redis 未启动或未加载 `.env` 时登录无法正常工作。

## 8. 日常启动顺序

首次配置完成后，每天开发时按以下顺序即可：

1. 启动 MySQL
2. 启动 Redis
3. 执行 `. .\use-java8.ps1` 和 `. .\use-local-secrets.ps1`
4. 启动后端 `ktg-admin.jar` 或运行 `RuoYiApplication`
5. 在根目录 `.env` 配置 `MODEL_API_KEY`、`MODEL_BASE_URL` 和 `MODEL_ID`
6. 在 `ktg-agent` 中执行 `uv run --locked python -m ktg_agent.server`
7. 在 `ktg-mes-ui` 中执行 `npm run dev -- --port 81`
8. 浏览器访问 `http://localhost:81`，通过公共布局右下角“智能查询”使用当前账号查询产品、库存和工单

## 9. 前后端打包

后端：

```powershell
Set-Location E:\ktg_agent\ktg_agent_v1
. .\use-java8.ps1
Set-Location .\ktg-mes-master\ktg-mes-master
mvn -s ..\..\maven-settings.xml clean package -DskipTests
```

产物：

```text
ktg-admin/target/ktg-admin.jar
```

前端：

```powershell
cd E:\ktg_agent\ktg_agent_v1\ktg-mes-ui
npm run build:prod
```

产物：

```text
ktg-mes-ui/dist/
```

生产环境需要由 Nginx 等 Web 服务器托管 `dist`，并把 `/prod-api/` 反向代理到后端 `8080`，代理时去掉 `/prod-api` 前缀；`/ureport/` 也应代理到后端。智能查询还要求把同源 `/agent-api/` 反向代理到 Agent `8001`，不要把模型 API Key 放进前端或 Nginx 配置。

## 10. 常见问题

### 后端编译提示 `org.omg.CORBA` 不存在

正在使用 JDK 11 或更高版本。该源码引用了 JDK 8 中的 CORBA 包，请切换到 JDK 8，并再次确认 `mvn -version` 显示的 Java 版本也是 1.8。

### 后端提示数据库连接失败

检查 MySQL 是否启动、数据库名是否为 `j2eedb`，以及 `application-druid.yml` 中的用户名和密码是否已替换。若出现 SSL 错误，将 JDBC 地址改为 `useSSL=false`。

### 登录、验证码或令牌操作失败

确认 Redis 已启动，并检查 `application.yml` 中的 Redis 地址、端口和密码。

### 页面能打开，但接口返回 404 或网络错误

先确认后端运行在 `8080`。开发环境还要确认前端使用 `.env.development`，其接口前缀为 `/dev-api`，并且 `vue.config.js` 的代理目标仍为 `http://localhost:8080`。

### 前端在高版本 Node.js 下安装或启动失败

切换到 Node.js 14 LTS 或 16 LTS，删除本次失败安装生成的 `node_modules` 后重新执行 `npm install`。该项目前端依赖较老，高版本 Node.js 并不是稳妥的运行环境。

### 前端端口被占用

换一个端口启动，例如：

```powershell
npm run dev -- --port 8081
```

### 文件上传失败

普通本地上传先检查 `ktg-mes.profile` 指定目录是否存在且可写；MinIO 上传则检查 MinIO 服务、存储桶和访问凭据。

## 11. 上线前必须修改的配置

- `application.yml` 中的 Token 密钥
- `application-druid.yml` 中的 Druid 控制台默认账号密码
- MySQL、Redis 和 MinIO 的真实凭据
- 上传目录
- Swagger 开关（生产环境建议关闭）
- `ktg-mes.demoEnabled` 演示模式开关

