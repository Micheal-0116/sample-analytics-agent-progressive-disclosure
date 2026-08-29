"""冷启动配置装载:从 Secrets Manager 拉运行配置 → 从 S3 同步知识树。

在 main.py 里**最先** import(先于 tools/db 读取配置)。设计原则(借鉴 lark 的验证做法):
  - 镜像里只烤一个非敏感指针 RUNTIME_SECRET_ID;所有会变的配置(桶名、工作组、
    目录名、治理角色 ARN、Bedrock 路由)都放在这个 Secrets Manager secret 里,冷启动时
    取一次 setdefault 进 os.environ(显式 env 优先)。
  - Runtime exec role 需要 secretsmanager:GetSecretValue(该 secret)+ s3:Get/List
    (知识桶)+ bedrock:InvokeModel* + sts:AssumeRole(AGENT_ROLE_ARN)。

数据层换成 Athena 之后,这里**不再有任何口令**:Athena / Glue / S3 Tables 都是
HTTPS + IAM 的 AWS API 调用,没有连接串、没有密码、不需要 VPC(那也是
agentcore.json 从 networkMode=VPC 改回 PUBLIC 的原因)。secret 里剩下的全是
非敏感的坐标——留着 Secrets Manager 这一层是因为这些坐标**会随环境变**,烤进镜像
就得为改一个桶名重建镜像。

secret JSON 期望键(全部非敏感,均可为空走默认):
  DB_BACKEND=athena  ATHENA_WORKGROUP  ATHENA_CATALOG  GLUE_CATALOG_ID
  ICEBERG_NAMESPACE  S3_TABLE_BUCKET  AGENT_ROLE_ARN  KNOWLEDGE_BUCKET
  AWS_REGION  CLAUDE_CODE_USE_BEDROCK  ANTHROPIC_MODEL

⚠️ ATHENA_CATALOG 与 GLUE_CATALOG_ID 是**同一个目录的两种写法**,不能混用:
  ATHENA_CATALOG   = s3tablescatalog/<表桶>            (Athena 用,不带账号前缀)
  GLUE_CATALOG_ID  = <账号>:s3tablescatalog/<表桶>     (boto3 glue 的 CatalogId,带前缀)
写串了报 EntityNotFoundException,而那个报错指不到成因。GLUE_CATALOG_ID 不设时
athena.py 会从 sts 现算,所以它其实可省;显式放进 secret 是为了让这两种写法在同一处
并排出现——省掉它的代价是下一个人得先读懂 athena.py 才知道有两种写法。

⚠️ AGENT_ROLE_ARN 是 L4 治理层那个最小权限角色(scripts/lakehouse/governance.py 建)。
**不设它就等于没有列级边界**:db.py 会直接用 Runtime exec role 的凭证查数,
user_messages / users.email / phone / user_profiles.birth_date 就都看得见了。
生效身份可以从 db.backend_info()['identity'] 读出来——那是从外面唯一能看见
"治理接上了没有"的地方。
"""
from __future__ import annotations

import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def _load_runtime_secret() -> None:
    sid = os.environ.get("RUNTIME_SECRET_ID", "")
    if not sid:
        logger.info("RUNTIME_SECRET_ID 未设置;沿用现有 env(本地开发)")
        return
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        data = json.loads(client.get_secret_value(SecretId=sid)["SecretString"])
        for k, v in data.items():
            os.environ.setdefault(k, str(v))
        # 只记键数,值(含口令)绝不写日志
        logger.info("已从 Secrets Manager 载入 %d 个运行配置键", len(data))
    except Exception as e:
        # 不吞。见文件末尾「为什么这里要硬失败」。
        raise RuntimeError(f"运行配置载入失败({sid});容器拒绝启动") from e


_load_runtime_secret()

# 配置就位后,把知识树从 S3 拉到本地(read_doc 读它)。延迟 import 让 knowledge_store
# 的 module 级配置读到已填好的 env(KNOWLEDGE_BUCKET 等)。
import knowledge_store  # noqa: E402 —— 必须在 _load_runtime_secret() 之后

_synced = knowledge_store.sync_down()
if knowledge_store.KNOWLEDGE_BUCKET and _synced == 0:
    # 设了桶却一个文件都没同步下来 = read_doc 读到的是空目录。sync_down() 自己
    # 不会因此报错(它对「没设桶」和「设了桶但一片空」返回同一个 0),所以在这里拦。
    raise RuntimeError(
        f"知识桶已设({knowledge_store.KNOWLEDGE_BUCKET})但同步到 0 个文件;容器拒绝启动"
    )

# ————————————————————— 为什么这里要硬失败 —————————————————————
# 这两处原来都是 logger.exception(...) 然后继续跑。2026-08-20 首次部署时踩到了:
# Runtime exec role 是部署之后才存在的,所以先起来的那批容器还没拿到
# secretsmanager:GetSecretValue,载入 secret 失败 → 记一笔日志继续服务。
#
# 后果不是"报错",而是**静默降级,而且是永久的**:module 级代码一个容器只跑一次,
# 补了 IAM 也不会重跑。那批容器在流量池里活了 20 分钟,期间:
#   · KNOWLEDGE_BUCKET 为空 → sync_down() 走「没设桶就跳过」返回 0 → read_doc
#     读空目录,agent 试了 16 个路径全是「文档不存在」,只好凭记忆猜字段
#   · AGENT_ROLE_ARN 为空 → db.py 直接用 exec role 查数,而 exec role 没有任何
#     数据面权限 → call_metric 报权限错
# 而调用方看到的是 HTTP 200、success: true、73 秒,答案里还从容地写着
# 「治理指标暂时报权限错,我走原始表手查」——读起来完全像个正常的分析过程。
#
# 一个连自己配置都没拿到的容器不该回答问题。在 import 期 raise 会让这个 microVM
# 起不来、永远不进流量池,请求转到配置齐全的容器上;全都起不来就是整体 5xx——
# 那是**该看见**的故障,比一个嘴上很利索、手里没有数据的 agent 好得多。
#
# 本地开发不受影响:RUNTIME_SECRET_ID / KNOWLEDGE_BUCKET 都不设时两个分支都不触发。
