#!/usr/bin/env python3
"""把 Redshift namespace 注册进 AWS Glue Data Catalog（federated catalog）。

## 这一步在架构里的位置

v2 把元数据拆成三态（见 docs/architecture-v2-redshift-glue.md 第二节）：

    database/*.sql + schema_manifest.yaml   ← 声明态（人写、进 git、走评审）
                │
                ▼  本脚本
      Glue Data Catalog                    ← 实际态（生成的，不可手改）
                │
    knowledge/domains/** 表卡片              ← 语义层（人的判断）

Glue 这一环**不是元数据真源**，它是「Redshift 里实际长成什么样」的机器可读投影，
供 scripts/glue/reconcile.py 跟声明态、语义层做三方对账。手写 md 的原罪不是格式，
是没人验证它；这一环存在的意义就是让验证有个客观参照。

## 三个容易踩的点（都是实测出来的）

**1. `lakeformation register-resource` 不能漏。** 官方文档给它标了「This is a
mandatory step」，但它夹在两段 console 操作说明之间很容易看漏。漏了它，第 5 步
`glue create-catalog` 会报 `Insufficient Lake Formation permission(s)`，错误信息
指向 datashare ARN，很容易误判成权限不足而去乱加 IAM。

**2. 建 catalog 必须是 Lake Formation data lake admin，且没有更窄的替代。**
`lakeformation grant-permissions` 只支持 9 种资源类型（Catalog / Database / Table /
TableWithColumns / DataLocation / DataCellsFilter / LFTag / LFTagPolicy /
LFTagExpression），**没有 DataShare 这一类**，所以无法只在那个 datashare ARN 上发一条
资源级授权。AdministratorAccess 的 IAM 权限也不够——Lake Formation 有独立的授权层。
这是个账号级权限，脚本里的追加逻辑刻意做成读-改-写并断言原有 admin 未丢失。

**3. `DataLakeAccess` 默认不开。** 官方明确：「用 Amazon Redshift 访问 federated
catalog **不需要**开 data lake access」。开了它会让 AWS Glue 额外拉起一个托管 Redshift
集群，用来支撑 Athena / EMR Spark 走 Iceberg REST 读写。本项目的 agent 是通过 Redshift
Data API 查数的，元数据只需要在 Glue 里可读，所以这个开关关掉：省掉一个常驻集群的钱，
也省掉 DataTransferRole 和 S3 暂存桶。想让 Athena 直读时再传 --data-transfer-role。

## 五个步骤，每步都幂等

1. LF data lake admin —— **追加不覆盖**。put-data-lake-settings 是整体替换语义，
   脚本先 get 再在原对象上追加本次身份，其余字段原样回填。丢掉别人的 admin 会是
   很难查的事故，所以写完还会断言原有 admin 仍在。
2. register-namespace —— Redshift 侧发布 namespace，产生 INTERNAL datashare。
3. associate-data-share-consumer —— 以 Glue catalog 为 consumer 接受邀请
   （状态 AUTHORIZED → ACTIVE）。
4. lakeformation register-resource —— 见上面第 1 点。
5. glue create-catalog —— Identifier 指向第 2 步的 datashare ARN。

## 建完之后的目录层级

Redshift federated catalog 比普通 Glue catalog **多一层**：

    <acct>:analytics_agent_rs                    ← create-catalog 建的顶层
      ├─ <acct>:analytics_agent_rs/dev           ← Redshift 自带的空默认库
      └─ <acct>:analytics_agent_rs/app_analytics ← Redshift 的 database
           └─ Glue database "public"             ← Redshift 的 schema
                └─ table

也就是 Redshift 的 *database* 成了子 catalog，Redshift 的 *schema* 才映射成 Glue 的
database。在顶层 catalog 上直接 get-databases 什么也取不到，必须先下潜一层。

用法：

    python3 scripts/glue/register_catalog.py            # 跑全部（幂等，可重复执行）
    python3 scripts/glue/register_catalog.py --verify   # 只验证，不做任何写操作
    python3 scripts/glue/register_catalog.py --steps 4,5
    python3 scripts/glue/register_catalog.py --revoke-admin   # 撤回 admin（先读下面）

## 撤回 admin 之前必须先发目录只读权限（本仓库尚未做这一步）

catalog 是用 `CreateDatabaseDefaultPermissions: []` /
`CreateTableDefaultPermissions: []` 建的——**刻意不给任何 principal 默认全权**。
而 data lake admin 会绕过 LF 的权限检查，所以只要还挂着 admin 身份，就看不出来
「非 admin 到底能不能读这个目录」。

直接 `--revoke-admin` 的后果：`reconcile.py` 大概率立刻读不到表，六类检查全变成
F 类（目录不可用）。要安全撤回，得先给跑对账的那个身份（开发者或 CI 角色）显式发三条
只读权限，`lakeformation grant-permissions` 分别打在：

    Catalog   <acct>:analytics_agent_rs                      DESCRIBE
    Database  <acct>:analytics_agent_rs/app_analytics  public DESCRIBE
    Table     同上 + TableWildcard {}                        SELECT, DESCRIBE

**这一步是有意留白的**：发权限是另一次 RBAC 变更，不在本次授权范围内。当前状态是
admin 身份保留、对账正常工作。要收紧成「最小权限 + 无常驻 admin」，需要先批准上面
那三条 grant，再跑 `--revoke-admin`。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = os.getenv("AWS_REGION", "ap-northeast-1")
NAMESPACE = os.getenv("REDSHIFT_NAMESPACE", "analytics-agent-ns")
WORKGROUP = os.getenv("REDSHIFT_WORKGROUP", "analytics-agent-wg")
# 官方建议 catalog 名用小写；它会成为 catalogid.dbName.schema.table 里的第一段。
CATALOG_NAME = os.getenv("GLUE_CATALOG_NAME", "analytics_agent_rs")

DESCRIPTION = "Redshift federated catalog for the analytics agent demo (metadata only)"
SHARE_SUFFIX = "/ds_internal_namespace"


def log(msg: str) -> None:
    print(msg, flush=True)


def caller_role_arn(sts) -> str:
    """把 assumed-role 会话 ARN 还原成底层 IAM role ARN。

    Lake Formation 的 principal 要 role ARN（arn:aws:iam::…:role/…），而
    get-caller-identity 回的是会话 ARN（arn:aws:sts::…:assumed-role/NAME/session）。
    直接拿会话 ARN 去 put-data-lake-settings 会被拒。
    """
    arn = sts.get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        return arn
    acct = arn.split(":")[4]
    role_name = arn.split(":assumed-role/")[1].split("/")[0]
    try:
        # SSO 角色带 /aws-reserved/sso.amazonaws.com/<region>/ 路径，必须查出来；
        # 手拼 arn:aws:iam::<acct>:role/<name> 对它是错的。
        return boto3.client("iam").get_role(RoleName=role_name)["Role"]["Arn"]
    except ClientError:
        return f"arn:aws:iam::{acct}:role/{role_name}"


def find_share(rs) -> str:
    for s in rs.describe_data_shares().get("DataShares", []):
        if s.get("DataShareArn", "").endswith(SHARE_SUFFIX):
            return s["DataShareArn"]
    return ""


# ------------------------------------------------------------------ steps

def step1_lf_admin(lf, me: str, verify: bool) -> None:
    cur = lf.get_data_lake_settings()["DataLakeSettings"]
    admins = [a["DataLakePrincipalIdentifier"] for a in cur.get("DataLakeAdmins", [])]
    log(f"[1] LF data lake admin 现有 {len(admins)} 个")
    for a in admins:
        log(f"      {a}")
    if me in admins:
        log("    本次身份已是 admin，跳过 ✅")
        return
    if verify:
        log(f"    ❌ 本次身份不是 admin：{me}")
        return
    cur["DataLakeAdmins"] = [{"DataLakePrincipalIdentifier": a}
                             for a in admins + [me]]
    lf.put_data_lake_settings(DataLakeSettings=cur)
    after = {a["DataLakePrincipalIdentifier"] for a in
             lf.get_data_lake_settings()["DataLakeSettings"]["DataLakeAdmins"]}
    missing = set(admins) - after
    if missing:
        raise SystemExit(f"原有 admin 被丢掉了，立即人工恢复：{sorted(missing)}")
    log(f"    已追加，现在 {len(after)} 个 admin ✅（原有 admin 已断言保留）")


def step2_register_namespace(rs, acct: str, verify: bool) -> str:
    arn = find_share(rs)
    if arn:
        log(f"[2] datashare 已存在，跳过 ✅\n      {arn}")
        return arn
    if verify:
        log("[2] ❌ 未找到 ds_internal_namespace datashare")
        return ""
    rs.register_namespace(
        NamespaceIdentifier={"ServerlessIdentifier": {
            "NamespaceIdentifier": NAMESPACE,
            "WorkgroupIdentifier": WORKGROUP,
        }},
        ConsumerIdentifiers=[acct],
    )
    for _ in range(30):
        time.sleep(2)
        arn = find_share(rs)
        if arn:
            log(f"[2] 已注册 ✅\n      {arn}")
            return arn
    raise SystemExit("[2] register-namespace 后等不到 datashare 出现")


def step3_associate_consumer(rs, acct: str, share_arn: str, verify: bool) -> None:
    consumer = f"arn:aws:glue:{REGION}:{acct}:catalog"
    d = rs.describe_data_shares(DataShareArn=share_arn)["DataShares"][0]
    assoc = d.get("DataShareAssociations", [])
    log(f"[3] associations: "
        f"{[(a.get('ConsumerIdentifier'), a.get('Status')) for a in assoc]}")
    if any(a.get("Status") == "ACTIVE" for a in assoc):
        log("    已 ACTIVE，跳过 ✅")
        return
    if verify:
        log("    （verify 模式，不做写操作）")
        return
    try:
        rs.associate_data_share_consumer(DataShareArn=share_arn,
                                         ConsumerArn=consumer)
        log("    已接受邀请，AUTHORIZED → ACTIVE ✅")
    except ClientError as e:
        if "already" in str(e).lower():
            log(f"    已关联，跳过（{e.response['Error']['Code']}）")
        else:
            raise


def step4_register_resource(lf, share_arn: str, verify: bool) -> None:
    try:
        lf.describe_resource(ResourceArn=share_arn)
        log("[4] LF register-resource 已存在，跳过 ✅")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    if verify:
        log("[4] ❌ LF 未注册该 datashare（第 5 步会报 Insufficient Lake Formation "
            "permission(s)）")
        return
    lf.register_resource(ResourceArn=share_arn)
    log("[4] 已注册到 Lake Formation ✅（官方标注 mandatory 的一步）")


def step5_create_catalog(glue, acct: str, share_arn: str, verify: bool,
                         data_transfer_role: str | None) -> None:
    cid = f"{acct}:{CATALOG_NAME}"
    try:
        c = glue.get_catalog(CatalogId=cid)["Catalog"]
        fed = c.get("FederatedCatalog", {})
        log(f"[5] catalog 已存在，跳过 ✅  {cid}\n"
            f"      Identifier      {fed.get('Identifier')}\n"
            f"      ConnectionName  {fed.get('ConnectionName')}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            log(f"[5] get-catalog: {e.response['Error']['Code']}")
    if verify:
        log(f"[5] ❌ catalog {cid} 不存在")
        return

    ci: dict = {
        "Description": DESCRIPTION,
        # 显式给空：不让任何 principal 默认拿到全权，权限一律走 LF grant 显式发放。
        "CreateDatabaseDefaultPermissions": [],
        "CreateTableDefaultPermissions": [],
        "FederatedCatalog": {
            "Identifier": share_arn,
            "ConnectionName": "aws:redshift",
        },
    }
    if data_transfer_role:
        # 只在要让 Athena / EMR 走 Iceberg REST 直读时才需要。见模块 docstring 第 3 点。
        ci["CatalogProperties"] = {"DataLakeAccessProperties": {
            "DataLakeAccess": True,
            "DataTransferRole": data_transfer_role,
        }}
    glue.create_catalog(Name=CATALOG_NAME, CatalogInput=ci)
    log(f"[5] 已创建 federated catalog ✅  {cid}")


def revoke_admin(lf, me: str) -> None:
    """撤回自己的 LF data lake admin 身份。

    建 catalog 是一次性动作，admin 是账号级权限，用完不必留着。同样是读-改-写，
    只摘掉自己那一条，并拒绝撤到零 admin（那会把 Lake Formation 锁死）。

    ⚠️ 先读模块 docstring 的「撤回 admin 之前必须先发目录只读权限」：admin 绕过 LF
    权限检查，撤掉之后如果没有显式的只读授权，reconcile.py 会立刻读不到目录。
    """
    cur = lf.get_data_lake_settings()["DataLakeSettings"]
    admins = [a["DataLakePrincipalIdentifier"] for a in cur.get("DataLakeAdmins", [])]
    if me not in admins:
        log(f"[revoke] 本次身份本就不是 admin，无需操作 ✅")
        return
    rest = [a for a in admins if a != me]
    if not rest:
        raise SystemExit("[revoke] 拒绝执行：撤掉后账号将没有任何 data lake admin，"
                         "会把 Lake Formation 锁死。请先指定其他 admin。")
    cur["DataLakeAdmins"] = [{"DataLakePrincipalIdentifier": a} for a in rest]
    lf.put_data_lake_settings(DataLakeSettings=cur)
    log(f"[revoke] 已撤回本次身份，剩余 admin {len(rest)} 个 ✅")
    for a in rest:
        log(f"      {a}")


# ------------------------------------------------------------------ verify

def verify_catalog(glue, acct: str) -> int:
    """列出 catalog 下的子 catalog / database / table，确认元数据真的到位。"""
    top = f"{acct}:{CATALOG_NAME}"
    log(f"\n=== 验证 {top} ===")
    try:
        kids = glue.get_catalogs(ParentCatalogId=top).get("CatalogList", [])
    except ClientError as e:
        log(f"get-catalogs 失败：{e}")
        return 1
    log(f"子 catalog（Redshift database 层）：{[c['CatalogId'] for c in kids]}")

    total = 0
    for k in kids:
        cid = k["CatalogId"]
        try:
            dbs = [d["Name"] for d in
                   glue.get_databases(CatalogId=cid).get("DatabaseList", [])]
        except ClientError as e:
            log(f"  {cid}: get-databases {e.response['Error']['Code']}")
            continue
        for db in dbs:
            tabs, tok = [], None
            while True:
                kw = {"CatalogId": cid, "DatabaseName": db}
                if tok:
                    kw["NextToken"] = tok
                r = glue.get_tables(**kw)
                tabs += r.get("TableList", [])
                tok = r.get("NextToken")
                if not tok:
                    break
            total += len(tabs)
            log(f"  {cid} / {db}: {len(tabs)} 张表")
    log(f"合计 {total} 张表")
    log("\n下一步：python3 scripts/glue/reconcile.py "
        f"--catalog {top} --strict")
    return 0 if total else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="注册 Redshift namespace 到 Glue Data Catalog")
    ap.add_argument("--steps", default="1,2,3,4,5", help="要执行的步骤，逗号分隔")
    ap.add_argument("--verify", action="store_true", help="只检查状态，不做写操作")
    ap.add_argument("--data-transfer-role",
                    help="开启 DataLakeAccess（Athena/EMR 直读）所需的 IAM role ARN。"
                         "不给则不开，见模块 docstring 第 3 点的取舍说明")
    ap.add_argument("--revoke-admin", action="store_true",
                    help="只撤回本次身份的 LF data lake admin（建完 catalog 后收尾用）")
    a = ap.parse_args()

    sts = boto3.client("sts", region_name=REGION)
    acct = sts.get_caller_identity()["Account"]
    me = caller_role_arn(sts)
    lf = boto3.client("lakeformation", region_name=REGION)
    rs = boto3.client("redshift", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)

    log(f"account={acct}  region={REGION}  catalog={CATALOG_NAME}"
        f"{'  （verify 模式）' if a.verify else ''}")
    log(f"caller={me}\n")

    if a.revoke_admin:
        revoke_admin(lf, me)
        return 0

    steps = {int(s) for s in a.steps.split(",") if s.strip()}
    if 1 in steps:
        step1_lf_admin(lf, me, a.verify)
    share_arn = step2_register_namespace(rs, acct, a.verify) if 2 in steps \
        else find_share(rs)
    if not share_arn:
        log("拿不到 datashare ARN，后续步骤跳过")
        return 1
    if 3 in steps:
        step3_associate_consumer(rs, acct, share_arn, a.verify)
    if 4 in steps:
        step4_register_resource(lf, share_arn, a.verify)
    if 5 in steps:
        step5_create_catalog(glue, acct, share_arn, a.verify, a.data_transfer_role)

    return verify_catalog(glue, acct)


if __name__ == "__main__":
    raise SystemExit(main())
