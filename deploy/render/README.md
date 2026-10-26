# Miles 的记忆搬上 Render 免费版（记忆存在你自己的 GitHub 私有仓库）

这一版 Ombre-Brain 在 Render 免费版上跑。免费版没有硬盘，一重启、一睡着，机器上的文件就全没了，所以记忆不存在 Render 上，存在你 GitHub 上一个**私有仓库**里：

- 开机时：先从私有仓库把记忆全部拉回来，拉不到就不开门（绝不会拿一个空库去盖掉你的记忆）。
- 平时：Miles 每存、改、删一条，两三秒内就推一次到仓库，只推变了的那几条。
- 关机时（睡着、重新部署）：最后再推一次。
- Sesame 的登录凭证也加密存在仓库里，所以 Render 睡醒以后不用重新授权。
- 防睡：北京时间早上 7 点到夜里 2 点，它每 10 分钟自己敲一下门，不让自己睡着；夜里 2 点到 7 点让它睡。一天醒 19 个小时，一个月大约 590 小时，在 Render 每月 750 小时的免费额度里。

代码在 `miles-render` 这个分支上，比原版多了 `src/miles_cloud.py`，`Dockerfile` 换成了 Render 用的（原版留在 `Dockerfile.upstream`）。

---

## 第一步：在 GitHub 建一个私有仓库，专门放记忆

1. 打开 https://github.com/new
2. Repository name 填 `miles-memory`（叫别的也行，后面对应改）。
3. 选 **Private**（私有）。一定要私有。
4. 勾上 **Add a README file**。
5. 点最下面绿色的 **Create repository**。

## 第二步：生成一个只管这个仓库的令牌

1. 打开 https://github.com/settings/personal-access-tokens/new
   （或者：右上角头像 → Settings → 左边最底下 Developer settings → Personal access tokens → Fine-grained tokens → Generate new token）
2. Token name 填 `miles-memory-render`。
3. Expiration（有效期）选最长的，或者 Custom 选一年后。到期前要换一个新的。
4. Repository access 选 **Only select repositories**，下拉里只勾 `miles-memory`。
5. Permissions 里找到 **Contents**（新版页面要先点 Add permissions，再勾 Contents），把它改成 **Read and write**。别的都不用动。
6. 点 **Generate token**，复制那串 `github_pat_` 开头的字。它只显示这一次，先找个地方存好。
   这串字等会儿是填进 Render 的，**不用发给 Miles，也别发给别人**。

## 第三步：把改好的代码放到你的 GitHub

1. 打开 https://github.com/P0luz/Ombre-Brain ，点右上角 **Fork** → **Create fork**（已经 Fork 过就跳过）。
2. 告诉 Miles 你 Fork 好了。Miles 要往你的 Fork 里推一个 `miles-render` 分支，需要用你的 GitHub 身份，到时候 Miles 会问你用哪种办法（比如你再生成一个只管这个 Fork、7 天过期的令牌，或者在浏览器里登你的账号），Miles 自己不会去登录。

## 第四步：在 Render 上建 Web Service

1. 打开 https://dashboard.render.com ，点 **New** → **Web Service**。
   不要选 Blueprint：仓库里原版的 render.yaml 是带付费硬盘的。
2. 第一次用要连 GitHub：点 GitHub，授权时选 **Only select repositories**，只选你的 `Ombre-Brain`。
3. 选 `Ombre-Brain` 仓库，然后填：
   - Name：`miles-memory`（网址会变成 `https://miles-memory-xxxx.onrender.com` 这样）
   - Language：**Docker**
   - Branch：**miles-render**
   - Region：**Oregon (US West)**。GitHub 的接口在美国西边，选这个读写记忆最快；你在国内只是授权那一下要打开网页，不影响。
   - Instance Type：**Free**
4. Environment Variables（环境变量）点 Add 填三个：

   | 名字 | 填什么 |
   |---|---|
   | `OMBRE_GITHUB_TOKEN` | 第二步那串 `github_pat_...` |
   | `MILES_MEMORY_REPO` | `你的GitHub用户名/miles-memory` |
   | `OMBRE_DASHBOARD_PASSWORD` | 你自己想一个 16 位以上的长密码。Sesame 授权时输它，也是记忆后台的登录密码。记好。 |

   不用填网址，Render 会自动告诉程序它自己的网址。
   想改防睡时段可以再加：`MILES_AWAKE_HOURS`（默认 `07-02`，北京时间几点到几点醒着），不想防睡就加 `MILES_KEEP_AWAKE` 填 `0`。
5. 点 **Deploy Web Service**。第一次构建要几分钟。看到 **Live** 就好了。
   左边 **Logs** 里应该能看到这几行：
   - `[miles-cloud] restored memories from 你的用户名/miles-memory: imported=0 ...`
   - `Uvicorn running on http://0.0.0.0:10000`
6. 页面左上角名字下面那个 `https://miles-memory-xxxx.onrender.com` 就是网址，复制下来。

## 第五步：在 Sesame 里把它加给 Miles

1. 在 Sesame 里点 Miles 的名字 → **Apps** → **Add custom app**。
2. 网址填 `https://miles-memory-xxxx.onrender.com/mcp`（就是上一步的网址，后面加 `/mcp`）。
3. 弹出授权页，输第四步的 `OMBRE_DASHBOARD_PASSWORD`，点允许。
4. 告诉 Miles 加好了，把网址也给 Miles。

如果它正睡着，第一次连可能要等一分钟左右；Sesame 要是说连不上，过一分钟再点一次就好。

## 第六步：Miles 来搬旧记忆（你不用动手）

Miles 用 `/home/sprite/mcp/miles_migrate_to_cloud.py` 把 VM 上现有的记忆原样搬上去（编号、时间、已消化的标记都不变），搬完逐条核对，再确认 GitHub 仓库里推上去了。之后 Miles 只用公网这一份，每晚的脚本也改成连它。VM 上的旧库留着不删，当备份。

---

## 出问题时看 Logs

| Logs 里看到 | 意思 | 怎么办 |
|---|---|---|
| `missing environment variables` | 三个变量少填了 | 去 Environment 补上 |
| `GitHub 拒绝了令牌（401）` | 令牌填错或过期 | 重新生成一个，换掉 `OMBRE_GITHUB_TOKEN` |
| `读不到记忆仓库（403/404）` | 仓库名写错，或者令牌没勾这个仓库 / Contents 不是 Read and write | 对一下第二步 |
| `refusing to start with an empty vault` | 读不到仓库，所以没开门（这是保护，不是坏了） | 按上面两行查 |
| `refusing to shrink the backup` | 一次少了一半以上的记忆，被拦住没推 | 告诉 Miles，先别动 |
| `pushed memory changes` | 正常：刚推了一次 | 不用管 |

## 几件要知道的事

- 记忆仓库千万别改成 Public。
- 令牌到期以后 Render 会读不到记忆、不开门，到期前换新的就行。
- Render 免费版不带“按意思找”的语义检索，搜记忆靠关键词。以后想要，可以再接一个向量接口。
- Render 偶尔会自己重启机器，没关系，记忆都在 GitHub 上。
