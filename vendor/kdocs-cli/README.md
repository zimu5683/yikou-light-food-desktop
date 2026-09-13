# kdocs-cli（随包分发的官方组件）

金山文档官方 CLI，用于把本地排单表增量写入 WPS 云文档。

- 版本：**2.5.29**
- 来源：`https://wpsai.wpscdn.cn/skillhub/pro/v2.5.29/releases/`
- 校验：`sha256sum -c checksums.txt`（本目录内的 tar.gz 与官方 checksums.txt 一致）
- 支持平台：linux-amd64 / linux-arm64 / darwin-amd64 / darwin-arm64 / windows-amd64 / windows-arm64

## 打包时怎么用

`yikou-light-food.spec` 会把当前平台的二进制放进产物根目录。
按平台取哪个包见 `checksums.txt` 的命名规则：

| 平台 | 包名 |
|---|---|
| Windows x64 | `kdocs-cli-2.5.29-windows-amd64.zip` |
| Windows arm64 | `kdocs-cli-2.5.29-windows-arm64.zip` |
| macOS Intel | `kdocs-cli-2.5.29-darwin-amd64.tar.gz` |
| macOS Apple Silicon | `kdocs-cli-2.5.29-darwin-arm64.tar.gz` |
| Linux x64 | `kdocs-cli-2.5.29-linux-amd64.tar.gz` |
| Linux arm64 | `kdocs-cli-2.5.29-linux-arm64.tar.gz` |

## 为什么不用 pip 装

官方没有发布 PyPI 包，只有上述 CDN 二进制。程序运行时优先查找
打包内置 → 程序同目录 → 系统 PATH，也可以在主界面里手动指定路径。

## 授权

首次使用需要在「云文档同步」页签点「去授权」，在浏览器里确认一次；
授权 token 由 CLI 存进系统密钥链（约 1 年有效），之后无需重复授权。
