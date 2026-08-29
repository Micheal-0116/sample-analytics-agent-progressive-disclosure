#!/usr/bin/env python3
r"""一次建好湖仓侧的全部基建：S3 桶、Athena workgroup、S3 表桶、Glue 联邦目录、LF 授权。

## 这个脚本存在的理由

这套基建原本是用 CLI 一条条敲出来的。手敲的基建等于**没有基建**：换个账号要重来，
出了问题不知道当初到底建成什么样，别人接手只能看聊天记录。所以八个步骤全部落成代码，
每一步幂等，`--verify` 只读不写。

它替代的是 v2 的 `scripts/glue/register_catalog.py`。那个脚本围绕 Redshift 的
datashare 展开（register-namespace → associate-consumer → register-resource →
create-catalog），随 Redshift 一起退役。S3 Tables 这条路少了 datashare 这一整层：
表桶本身就是 Iceberg 目录，联邦进 Glue 之后 namespace 直接映射成 Glue database。

## 目录层级：比 Redshift 联邦目录少一层

Redshift 联邦目录多一级（Redshift 的 database 成了子 catalog，schema 才是 Glue
database）。S3 Tables 没有这一层：

    <账号>:s3tablescatalog                           ← create-catalog 建的顶层
      └─ <账号>:s3tablescatalog/<表桶名>              ← 每个表桶一个子 catalog
           └─ Glue database "app_analytics"          ← Iceberg namespace 直接映射
                └─ table

`backend/catalog.py` 改的就是这个差异：少一层下潜。

## 六个实测踩出来的点

**1. 联邦目录不吃 `IAM_ALLOWED_PRINCIPALS` 向后兼容。** 这是本次最值钱的一条。
账号级 LF settings 里明明写着

    CreateDatabaseDefaultPermissions: [{IAM_ALLOWED_PRINCIPALS: [ALL]}]
    CreateTableDefaultPermissions:    [{IAM_ALLOWED_PRINCIPALS: [ALL]}]

而且当前身份是 data lake admin，第一次 `CREATE TABLE` 照样被拒：

    Iceberg cannot access the requested resource: Forbidden:
    Insufficient Lake Formation permission(s): Required Create Table on app_analytics

原因是**目录级**的默认权限是空的（第 5 步刻意传 `[]`），账号级那两条默认值不会
下渗到联邦目录。所以第 7 步的显式 grant 不是可选项。

**2. `FederatedCatalog.Identifier` 写 `bucket/*` 而不是具体桶 ARN。** 一次注册覆盖
账号内所有 S3 表桶，将来新建表桶不用再回来跑第 5 步。写死具体桶 ARN 也能用，但
每加一个桶就多一个目录，Athena 侧的 catalog 名也会变。

**3. 目录名用 `s3tablescatalog` 是刻意的。** 控制台上那个「一键集成 S3 Tables」按钮
建出来的目录就叫这个名。同名的话，将来谁去点了那个按钮不会凭空多出第二个目录，
点完还是这一个。名字本身没有强制要求，但换名字等于给自己埋一个未来的分叉。

**4. `BytesScannedCutoffPerQuery` 是这套架构里唯一的成本护栏。** Athena 按扫描字节
计费，没有空闲成本，但也没有上限——一条写错的 SQL（比如漏了 join 条件）能扫掉很多钱。
配上 `EnforceWorkGroupConfiguration=true`，单条查询没法绕过它。这两个值必须在代码里，
不能靠「记得去控制台设一下」。默认 1 GiB；这批数据全表扫也就几十 MB，撞到上限说明
SQL 有问题，正是想要的信号。

**5. `create_bucket` 在 us-east-1 不能传 `LocationConstraint`。** 经典坑，传了报
`InvalidLocationConstraint`。本项目默认 us-west-2，但脚本要能换区。

**6. Athena 结果暂存不单开桶**，放在原始桶的 `athena-staging/` 前缀下。少一个桶要
管、要授权、要记名字。代价是这个前缀会一直长——`--staging-expire-days` 可以给它加
生命周期规则，但**默认不开**：那是个会删对象的策略，得由人明确要求。

## 授权现状：这批 grant 其实还没被验证过

当前身份是 data lake admin，**LF 的权限检查被整体绕过**。所以第 7 步发的那些权限
到底够不够用，现在看不出来——`load.py` 靠 `DELETE FROM` 做幂等，而线上原本的
grant 里压根没有 `DELETE`，照样跑通了 19 万行。本脚本把 `DELETE` 补进清单（自测
里有一条断言把它和 load.py 的用法绑在一起），但真正的验证要等治理层撤掉 admin
之后才做得到。在那之前，这份清单是**声明，不是证明**。

治理层（给 agent 单独一个最小权限角色、PII 列掩码）不在本次范围内。`--principal`
预留了给别的身份发同一批权限的入口。

用法：

    python3 scripts/lakehouse/setup.py --selftest      # 自测（无云依赖）
    python3 scripts/lakehouse/setup.py --verify        # 只读，报告每一步的现状
    python3 scripts/lakehouse/setup.py                 # 建（幂等，可反复跑）
    python3 scripts/lakehouse/setup.py --steps 5,7     # 只跑某几步
    python3 scripts/lakehouse/setup.py --principal arn:aws:iam::…:role/agent  # 只发权限

建完之后的顺序：

    python3 scripts/lakehouse/gen_ddl.py --check       # 声明态是否跟真源一致
    python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql
    python3 scripts/lakehouse/load.py                  # 35 张基表灌数
    python3 scripts/lakehouse/athena.py --file database/iceberg/02_mart.sql
    python3 scripts/lakehouse/verify_mart_parity.py --numbers
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import boto3
from botocore.exceptions import ClientError

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

# 桶名/目录名/namespace 只有一处定义，从 athena.py 借。两处各写一遍迟早会漂，
# 而漂了的表现是 EntityNotFoundException，看不出是名字不一致。
import athena  # noqa: E402  import 时不发任何 AWS 调用，--selftest 保持无云

REGION = athena.REGION
RAW_BUCKET = athena.RAW_BUCKET
WORKGROUP = athena.WORKGROUP
TABLE_BUCKET = athena.TABLE_BUCKET
NAMESPACE = athena.NAMESPACE

STAGING_PREFIX = "athena-staging/"
CSV_PREFIX = "csv/"
RAW_GLUE_DB = os.environ.get("RAW_GLUE_DB", "analytics_agent_raw")
CATALOG_NAME = "s3tablescatalog"          # 见坑 3

# 成本护栏，见坑 4。单位字节；Athena 的下限是 10 MB。
BYTES_CUTOFF = int(os.environ.get("ATHENA_BYTES_CUTOFF", 1024 ** 3))

WG_DESC = "Analytics agent · S3 Tables (Iceberg) via Athena"
CATALOG_DESC = "S3 Tables federated catalog (analytics agent)"
RAW_DB_DESC = "CSV 落地区，只用于灌数中转"

# 第 7 步发的权限。DATABASE 的这四条支撑建表/改表/删表 + DESCRIBE。
DATABASE_PERMS = ["CREATE_TABLE", "DESCRIBE", "ALTER", "DROP"]
# TABLE 的这六条对应实际用到的操作：
#   SELECT/DESCRIBE  查数与元数据（backend 全靠它）
#   INSERT           load.py 灌数、02_mart.sql 的 INSERT INTO ... SELECT
#   DELETE           load.py 的幂等（每张表灌前 DELETE FROM）—— 线上原本漏了这条
#   ALTER/DROP       重建表、探针清理
TABLE_PERMS = ["SELECT", "DESCRIBE", "INSERT", "DELETE", "ALTER", "DROP"]

STEPS = (1, 2, 3, 4, 5, 6, 7, 8)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 身份

def caller_role_arn(sts, iam=None) -> str:
    """把 assumed-role 会话 ARN 还原成底层 IAM role ARN。

    Lake Formation 的 principal 要 role ARN（`arn:aws:iam::…:role/…`），而
    get-caller-identity 回的是会话 ARN（`arn:aws:sts::…:assumed-role/NAME/session`）。
    直接拿会话 ARN 去 put-data-lake-settings 会被拒。

    SSO 角色带 `/aws-reserved/sso.amazonaws.com/<region>/` 路径，手拼是错的，
    要查 IAM；查不到（没有 iam:GetRole）才退回手拼。
    """
    arn = sts.get_caller_identity()["Arn"]
    return normalize_role_arn(arn, iam)


def normalize_role_arn(arn: str, iam=None) -> str:
    if ":assumed-role/" not in arn:
        return arn
    acct = arn.split(":")[4]
    role = arn.split(":assumed-role/")[1].split("/")[0]
    if iam is not None:
        try:
            return iam.get_role(RoleName=role)["Role"]["Arn"]
        except ClientError:
            pass
    return f"arn:aws:iam::{acct}:role/{role}"


def create_bucket_kwargs(bucket: str, region: str) -> dict:
    """us-east-1 不能传 LocationConstraint，见坑 5。"""
    kw = {"Bucket": bucket}
    if region != "us-east-1":
        kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
    return kw


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


# ---------------------------------------------------------------- 1 原始桶

def step1_raw_bucket(s3, verify: bool, staging_expire_days: int) -> int:
    exists = True
    try:
        s3.head_bucket(Bucket=RAW_BUCKET)
    except ClientError as e:
        if _code(e) in ("404", "NoSuchBucket"):
            exists = False
        elif _code(e) == "403":
            log(f"[1] 桶 {RAW_BUCKET} 存在但当前身份读不到（403）—— 可能是别人的桶名 ❌")
            return 1
        else:
            raise

    if not exists:
        if verify:
            log(f"[1] ❌ 桶 {RAW_BUCKET} 不存在")
            return 1
        s3.create_bucket(**create_bucket_kwargs(RAW_BUCKET, REGION))
        log(f"[1] 建桶 {RAW_BUCKET}（{REGION}）")
    else:
        log(f"[1] 桶 {RAW_BUCKET} 已存在")

    bad = 0
    # 默认加密。S3 现在本来就默认 AES256，显式写一遍是为了「配置在代码里可读」。
    enc = None
    try:
        enc = s3.get_bucket_encryption(Bucket=RAW_BUCKET)[
            "ServerSideEncryptionConfiguration"]["Rules"][0][
            "ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
    except ClientError as e:
        if _code(e) != "ServerSideEncryptionConfigurationNotFoundError":
            raise
    if enc:
        log(f"      加密 {enc} ✅")
    elif verify:
        log("      ❌ 没有默认加密")
        bad += 1
    else:
        s3.put_bucket_encryption(
            Bucket=RAW_BUCKET,
            ServerSideEncryptionConfiguration={"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        log("      已设默认加密 AES256")

    # 阻断公开访问。桶里是业务数据 + 查询结果，公开一次就收不回来。
    pab = {}
    try:
        pab = s3.get_public_access_block(Bucket=RAW_BUCKET)[
            "PublicAccessBlockConfiguration"]
    except ClientError as e:
        if _code(e) != "NoSuchPublicAccessBlockConfiguration":
            raise
    want_pab = {"BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
    if all(pab.get(k) for k in want_pab):
        log("      公开访问已全部阻断 ✅")
    elif verify:
        log(f"      ❌ 公开访问未全部阻断：{pab or '(未配置)'}")
        bad += 1
    else:
        s3.put_public_access_block(Bucket=RAW_BUCKET,
                                   PublicAccessBlockConfiguration=want_pab)
        log("      已阻断全部公开访问")

    # 生命周期：默认不动，见坑 6。这一条会删对象，必须由人明确要求。
    if staging_expire_days > 0 and not verify:
        s3.put_bucket_lifecycle_configuration(
            Bucket=RAW_BUCKET,
            LifecycleConfiguration={"Rules": [{
                "ID": "expire-athena-staging",
                "Status": "Enabled",
                "Filter": {"Prefix": STAGING_PREFIX},
                "Expiration": {"Days": staging_expire_days},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }]})
        log(f"      已给 {STAGING_PREFIX} 加 {staging_expire_days} 天过期规则"
            f"（只影响这个前缀，csv/ 不受影响）")
    return bad


# ---------------------------------------------------------------- 2 workgroup

def step2_workgroup(ath, verify: bool) -> int:
    out = f"s3://{RAW_BUCKET}/{STAGING_PREFIX}"
    want = {
        "ResultConfiguration": {"OutputLocation": out},
        "EnforceWorkGroupConfiguration": True,
        "PublishCloudWatchMetricsEnabled": True,
        "BytesScannedCutoffPerQuery": BYTES_CUTOFF,
        "EngineVersion": {"SelectedEngineVersion": "AUTO"},
    }
    try:
        wg = ath.get_work_group(WorkGroup=WORKGROUP)["WorkGroup"]
    except ClientError as e:
        if _code(e) != "InvalidRequestException":
            raise
        if verify:
            log(f"[2] ❌ workgroup {WORKGROUP} 不存在")
            return 1
        ath.create_work_group(Name=WORKGROUP, Description=WG_DESC,
                              Configuration=want)
        log(f"[2] 建 workgroup {WORKGROUP}"
            f"（engine v3 / 扫描上限 {BYTES_CUTOFF // 1024 ** 2} MiB / 强制生效）")
        return 0

    cfg = wg.get("Configuration", {})
    eff = cfg.get("EngineVersion", {}).get("EffectiveEngineVersion", "?")
    log(f"[2] workgroup {WORKGROUP} 已存在（{eff}，状态 {wg.get('State')}）")

    bad = 0
    got_out = cfg.get("ResultConfiguration", {}).get("OutputLocation")
    if got_out != out:
        log(f"      ⚠️  结果位置是 {got_out}，本脚本期望 {out}")
    # 护栏漂了要说话。这是唯一的成本上限，被人关掉而没人发现是最坏的情况。
    if not cfg.get("EnforceWorkGroupConfiguration"):
        log("      ❌ EnforceWorkGroupConfiguration=false，单条查询可以绕过扫描上限")
        bad += 1
    got_cut = cfg.get("BytesScannedCutoffPerQuery")
    if got_cut != BYTES_CUTOFF:
        log(f"      ⚠️  扫描上限是 {got_cut}，本脚本期望 {BYTES_CUTOFF}")
        if got_cut is None:
            log("         （None = 没有上限，成本护栏等于没有）")
            bad += 1
    else:
        log(f"      扫描上限 {BYTES_CUTOFF // 1024 ** 2} MiB，强制生效 ✅")

    if bad and not verify:
        # 只补护栏这一项，不整体覆盖别人可能改过的其它配置。
        ath.update_work_group(WorkGroup=WORKGROUP, ConfigurationUpdates={
            "EnforceWorkGroupConfiguration": True,
            "BytesScannedCutoffPerQuery": BYTES_CUTOFF,
        })
        log("      已补回成本护栏")
        return 0
    return bad


# ---------------------------------------------------------------- 3 LF admin

def step3_lf_admin(lf, me: str, verify: bool) -> int:
    """追加不覆盖。put_data_lake_settings 是整体替换语义，丢掉别人的 admin
    会是很难查的事故：症状是别人突然读不到目录，而这里的改动早就没人记得了。
    所以先 get、在原对象上追加、写完再断言原有 admin 仍在。"""
    cur = lf.get_data_lake_settings()["DataLakeSettings"]
    admins = [a["DataLakePrincipalIdentifier"] for a in cur.get("DataLakeAdmins", [])]
    log(f"[3] LF data lake admin 现有 {len(admins)} 个")
    for a in admins:
        log(f"      {a}{'   ← 本次身份' if a == me else ''}")
    if me in admins:
        log("      本次身份已是 admin ✅")
        return 0
    if verify:
        log(f"      ❌ 本次身份不是 admin：{me}")
        return 1
    cur["DataLakeAdmins"] = [{"DataLakePrincipalIdentifier": a}
                             for a in admins + [me]]
    lf.put_data_lake_settings(DataLakeSettings=cur)
    after = {a["DataLakePrincipalIdentifier"] for a in
             lf.get_data_lake_settings()["DataLakeSettings"]["DataLakeAdmins"]}
    lost = set(admins) - after
    if lost:
        raise SystemExit(f"原有 admin 被丢掉了，立即人工恢复：{sorted(lost)}")
    log(f"      已追加，现在 {len(after)} 个 ✅（原有 admin 已断言保留）")
    return 0


# ---------------------------------------------------------------- 4 表桶

def table_bucket_arn(acct: str) -> str:
    return f"arn:aws:s3tables:{REGION}:{acct}:bucket/{TABLE_BUCKET}"


def step4_table_bucket(s3t, acct: str, verify: bool) -> int:
    arn = table_bucket_arn(acct)
    try:
        b = s3t.get_table_bucket(tableBucketARN=arn)
        log(f"[4] S3 表桶已存在：{b['name']}（建于 {b.get('createdAt')}）")
    except ClientError as e:
        if _code(e) not in ("NotFoundException", "ResourceNotFoundException"):
            raise
        if verify:
            log(f"[4] ❌ S3 表桶 {TABLE_BUCKET} 不存在")
            return 1
        s3t.create_table_bucket(name=TABLE_BUCKET)
        log(f"[4] 建 S3 表桶 {TABLE_BUCKET}")

    try:
        enc = s3t.get_table_bucket_encryption(tableBucketARN=arn)[
            "encryptionConfiguration"].get("sseAlgorithm")
        log(f"      加密 {enc} ✅")
    except ClientError as e:
        log(f"      加密读不到：{_code(e)}")
    try:
        m = s3t.get_table_bucket_maintenance_configuration(
            tableBucketARN=arn)["configuration"]
        for k, v in m.items():
            log(f"      维护 {k}={v.get('status')} "
                f"{v.get('settings', {}).get(k, '')}")
    except ClientError:
        pass
    return 0


# ---------------------------------------------------------------- 5 联邦目录

def step5_catalog(glue, acct: str, verify: bool) -> int:
    try:
        c = glue.get_catalog(CatalogId=CATALOG_NAME)["Catalog"]
        fed = c.get("FederatedCatalog", {})
        log(f"[5] Glue 联邦目录已存在：{c['CatalogId']}")
        log(f"      Identifier     {fed.get('Identifier')}")
        log(f"      ConnectionName {fed.get('ConnectionName')}")
        # 目录级默认权限必须是空的，见坑 1。非空说明有人放宽了。
        for k in ("CreateDatabaseDefaultPermissions",
                  "CreateTableDefaultPermissions"):
            v = c.get(k, [])
            log(f"      {k} = {v or '[]'}"
                + ("   ← 刻意留空，权限一律走第 7 步显式发放" if not v
                   else "   ⚠️  非空，有人放宽了默认权限"))
        return 0
    except ClientError as e:
        if _code(e) not in ("EntityNotFoundException",):
            raise
    if verify:
        log(f"[5] ❌ Glue 联邦目录 {CATALOG_NAME} 不存在")
        return 1
    glue.create_catalog(Name=CATALOG_NAME, CatalogInput={
        "Description": CATALOG_DESC,
        "FederatedCatalog": {
            # 见坑 2：`bucket/*` 一次覆盖账号内所有表桶
            "Identifier": f"arn:aws:s3tables:{REGION}:{acct}:bucket/*",
            "ConnectionName": "aws:s3tables",
        },
        # 显式给空：不让任何 principal 默认拿到全权，权限一律走 LF grant。
        "CreateDatabaseDefaultPermissions": [],
        "CreateTableDefaultPermissions": [],
    })
    log(f"[5] 建 Glue 联邦目录 {CATALOG_NAME} → s3tables bucket/*")
    return 0


# ---------------------------------------------------------------- 6 namespace

def step6_namespace(s3t, acct: str, verify: bool) -> int:
    arn = table_bucket_arn(acct)
    try:
        have = [n["namespace"][0]
                for n in s3t.list_namespaces(tableBucketARN=arn)["namespaces"]]
    except ClientError as e:
        log(f"[6] ❌ 列 namespace 失败（表桶还没建？）：{_code(e)}")
        return 1
    if NAMESPACE in have:
        n = len(s3t.list_tables(tableBucketARN=arn, maxTables=1000)["tables"])
        log(f"[6] namespace {NAMESPACE} 已存在（表桶内共 {n} 张表）")
        other = [x for x in have if x != NAMESPACE]
        if other:
            log(f"      同桶另有 namespace：{other}"
                f"（`default` 是 Athena 建的空库，不用管）")
        return 0
    if verify:
        log(f"[6] ❌ namespace {NAMESPACE} 不存在（现有 {have}）")
        return 1
    s3t.create_namespace(tableBucketARN=arn, namespace=[NAMESPACE])
    log(f"[6] 建 namespace {NAMESPACE}"
        f"（它会自动映射成 Glue database，不用再建一遍）")
    return 0


# ---------------------------------------------------------------- 7 LF 授权

def step7_grants(lf, acct: str, principal: str, verify: bool) -> int:
    """给 principal 发 database + table 通配两组权限，见坑 1 和「授权现状」。"""
    cid = f"{acct}:{CATALOG_NAME}/{TABLE_BUCKET}"
    targets = [
        ("Database", {"Database": {"CatalogId": cid, "Name": NAMESPACE}},
         DATABASE_PERMS),
        ("Table", {"Table": {"CatalogId": cid, "DatabaseName": NAMESPACE,
                             "TableWildcard": {}}}, TABLE_PERMS),
    ]
    bad = 0
    log(f"[7] LF 授权 → {principal}")
    for label, resource, want in targets:
        try:
            got: set[str] = set()
            for p in lf.list_permissions(Resource=resource)[
                    "PrincipalResourcePermissions"]:
                if p["Principal"]["DataLakePrincipalIdentifier"] == principal:
                    got |= set(p["Permissions"])
        except ClientError as e:
            log(f"      ❌ {label} 读权限失败：{_code(e)} "
                f"{' '.join(str(e).split())[:120]}")
            bad += 1
            continue
        missing = [p for p in want if p not in got]
        if not missing:
            log(f"      {label:<9} {sorted(got)} ✅")
            continue
        if verify:
            log(f"      ❌ {label:<9} 缺 {missing}（现有 {sorted(got) or '无'}）")
            bad += 1
            continue
        lf.grant_permissions(
            Principal={"DataLakePrincipalIdentifier": principal},
            Resource=resource, Permissions=want)
        log(f"      {label:<9} 已补发 {missing}")
    log("      注：当前身份是 data lake admin，LF 检查被绕过 —— 这批权限"
        "够不够用要等治理层撤掉 admin 才验证得到")
    return bad


# ---------------------------------------------------------------- 8 CSV 中转库

def step8_raw_glue_db(glue, verify: bool) -> int:
    """CSV 外部表所在的普通 Glue 库。它是**唯一的原始态**，灌完别删：
    对账时能拿它和 Iceberg 侧逐值比。"""
    try:
        d = glue.get_database(Name=RAW_GLUE_DB)["Database"]
        n = len(glue.get_tables(DatabaseName=RAW_GLUE_DB,
                                MaxResults=1000).get("TableList", []))
        log(f"[8] Glue 库 {RAW_GLUE_DB} 已存在"
            f"（{n} 张 CSV 外部表，{d.get('Description', '')}）")
        return 0
    except ClientError as e:
        if _code(e) != "EntityNotFoundException":
            raise
    if verify:
        log(f"[8] ❌ Glue 库 {RAW_GLUE_DB} 不存在")
        return 1
    glue.create_database(DatabaseInput={"Name": RAW_GLUE_DB,
                                        "Description": RAW_DB_DESC})
    log(f"[8] 建 Glue 库 {RAW_GLUE_DB}（load.py 会往里建 *_csv 外部表）")
    return 0


# ---------------------------------------------------------------- 自测

def selftest() -> int:
    bad = 0

    # 会话 ARN → role ARN
    cases = [
        ("arn:aws:sts::123456789012:assumed-role/lake-admin/lake-admin-session",
         "arn:aws:iam::123456789012:role/lake-admin"),
        ("arn:aws:iam::123456789012:role/lake-admin",
         "arn:aws:iam::123456789012:role/lake-admin"),
        ("arn:aws:iam::123456789012:user/alice",
         "arn:aws:iam::123456789012:user/alice"),
    ]
    for src, want in cases:
        got = normalize_role_arn(src, iam=None)
        if got != want:
            print(f"  ❌ normalize_role_arn({src}) → {got}，期望 {want}")
            bad += 1

    # us-east-1 不能传 LocationConstraint，见坑 5
    if "CreateBucketConfiguration" in create_bucket_kwargs("b", "us-east-1"):
        print("  ❌ us-east-1 仍然传了 LocationConstraint")
        bad += 1
    if create_bucket_kwargs("b", "us-west-2").get(
            "CreateBucketConfiguration", {}).get("LocationConstraint") != "us-west-2":
        print("  ❌ 非 us-east-1 没带上 LocationConstraint")
        bad += 1

    # 常量必须跟 athena.py 是同一份（不是各写一遍）
    for name in ("REGION", "RAW_BUCKET", "WORKGROUP", "TABLE_BUCKET", "NAMESPACE"):
        if globals()[name] is not getattr(athena, name):
            print(f"  ❌ {name} 跟 athena.py 不是同一个对象，两处定义会漂")
            bad += 1

    # 护栏不能被无意调低到无效值：Athena 的下限是 10 MB
    if BYTES_CUTOFF < 10 * 1024 ** 2:
        print(f"  ❌ 扫描上限 {BYTES_CUTOFF} 低于 Athena 允许的 10 MB 下限")
        bad += 1

    # 权限清单要跟实际用到的 SQL 动作绑住。load.py 的幂等靠 DELETE FROM，
    # 而线上原本的 grant 里没有 DELETE —— 只因为 admin 绕过检查才没暴露。
    # 这条断言把「代码用了什么」和「权限发了什么」钉在一起。
    load_src = open(os.path.join(_HERE, "load.py"), encoding="utf-8").read()
    used = {"DELETE": r"\bDELETE\s+FROM\b", "INSERT": r"\bINSERT\s+INTO\b",
            "SELECT": r"\bSELECT\b"}
    for perm, rx in used.items():
        if re.search(rx, load_src, re.IGNORECASE) and perm not in TABLE_PERMS:
            print(f"  ❌ load.py 用了 {perm}，但 TABLE_PERMS 里没发这个权限")
            bad += 1

    # 目录 ID 的两种形式别写反了（混用的报错是 EntityNotFoundException，看不出原因）
    if not athena.ATHENA_CATALOG.startswith(f"{CATALOG_NAME}/"):
        print(f"  ❌ ATHENA_CATALOG={athena.ATHENA_CATALOG} 不是 "
              f"{CATALOG_NAME}/<表桶> 形式")
        bad += 1
    if ":" in athena.ATHENA_CATALOG:
        print("  ❌ ATHENA_CATALOG 带了账号前缀；那是 glue CatalogId 的形式")
        bad += 1

    if bad:
        print(f"\n{bad} 项失败 ❌")
        return 1
    print(f"  ARN 归一 {len(cases)} 例、建桶参数 2 例、常量同源 5 项、"
          f"护栏下限 1 项、权限与用法绑定 {len(used)} 项、目录 ID 形式 2 项")
    print("全部通过 ✅")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(
        description="湖仓侧基建：S3 桶 / Athena workgroup / S3 表桶 / Glue 联邦目录 / LF 授权")
    ap.add_argument("--verify", action="store_true", help="只读，不做任何写操作")
    ap.add_argument("--selftest", action="store_true", help="自测（无云依赖）")
    ap.add_argument("--steps", help=f"只跑某几步，逗号分隔（默认 {','.join(map(str, STEPS))}）")
    ap.add_argument("--principal", help="第 7 步授权给谁（默认当前身份的 role ARN）")
    ap.add_argument("--staging-expire-days", type=int, default=0,
                    help="给 athena-staging/ 加 N 天过期规则。**默认 0=不开**："
                         "这是个会删对象的策略，见模块 docstring 坑 6")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    steps = STEPS
    if a.steps:
        steps = tuple(int(x) for x in a.steps.split(",") if x.strip())

    sts = boto3.client("sts", region_name=REGION)
    acct = sts.get_caller_identity()["Account"]
    me = a.principal or caller_role_arn(sts, boto3.client("iam"))
    mode = "只读校验" if a.verify else "创建/补齐"
    log(f"账号 {acct}  区域 {REGION}  身份 {me}\n模式：{mode}"
        f"{'' if not a.steps else f'  步骤 {list(steps)}'}\n")

    s3 = boto3.client("s3", region_name=REGION)
    ath = boto3.client("athena", region_name=REGION)
    lf = boto3.client("lakeformation", region_name=REGION)
    s3t = boto3.client("s3tables", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)

    bad = 0
    if 1 in steps:
        bad += step1_raw_bucket(s3, a.verify, a.staging_expire_days)
    if 2 in steps:
        bad += step2_workgroup(ath, a.verify)
    if 3 in steps:
        bad += step3_lf_admin(lf, me, a.verify)
    if 4 in steps:
        bad += step4_table_bucket(s3t, acct, a.verify)
    if 5 in steps:
        bad += step5_catalog(glue, acct, a.verify)
    if 6 in steps:
        bad += step6_namespace(s3t, acct, a.verify)
    if 7 in steps:
        bad += step7_grants(lf, acct, me, a.verify)
    if 8 in steps:
        bad += step8_raw_glue_db(glue, a.verify)

    if bad:
        log(f"\n{bad} 项不符 ❌" + ("（--verify 只报告，不修；去掉它会补齐）"
                                   if a.verify else ""))
        return 1
    log("\n基建齐备 ✅\n下一步："
        "\n  python3 scripts/lakehouse/gen_ddl.py --check"
        "\n  python3 scripts/lakehouse/athena.py --file database/iceberg/01_tables.sql"
        "\n  python3 scripts/lakehouse/load.py"
        "\n  python3 scripts/lakehouse/athena.py --file database/iceberg/02_mart.sql"
        "\n  python3 scripts/lakehouse/verify_mart_parity.py --numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
