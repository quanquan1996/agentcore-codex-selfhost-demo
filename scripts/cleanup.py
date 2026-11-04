"""Delete everything deploy.py created (reads .state.json). Safe to re-run.
Usage: ./cleanup.sh"""
import json, os, sys, time
import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
STATE_FILE = os.path.join(HERE, ".state.json")
PREFIX = "codex-demo"
if not os.path.exists(STATE_FILE):
    sys.exit("nothing to clean up")
state = json.load(open(STATE_FILE))
region = state.get("region", "us-east-1")
se = boto3.Session(region_name=region)
ec2, iam, s3 = se.client("ec2"), se.client("iam"), se.client("s3")
s3files, lam, apigw = se.client("s3files"), se.client("lambda"), se.client("apigatewayv2")
ac, ecr = se.client("bedrock-agentcore-control"), se.client("ecr")
cb, logs = se.client("codebuild"), se.client("logs")


def tryit(desc, fn, *a, **k):
    """True if the resource is gone (deleted now or already missing)."""
    try:
        fn(*a, **k)
        print("deleted", desc)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        missing = "NotFound" in code or "NoSuch" in code
        print(("gone   " if missing else "SKIP   ") + desc, code)
        return missing


def gone(fn, *a, **k):
    for _ in range(90):
        try:
            fn(*a, **k)
        except ClientError:
            return
        time.sleep(10)


g = state.get
if g("runtime_id"):
    tryit("runtime", ac.delete_agent_runtime, agentRuntimeId=g("runtime_id"))
    gone(ac.get_agent_runtime, agentRuntimeId=g("runtime_id"))
if g("ecr_repo"):
    tryit("ecr repo", ecr.delete_repository, repositoryName=g("ecr_repo"), force=True)
if g("codebuild_project"):
    tryit("codebuild project", cb.delete_project, name=g("codebuild_project"))
if g("build_bucket"):
    try:
        se.resource("s3").Bucket(g("build_bucket")).objects.delete()
    except ClientError:
        pass
    tryit("build bucket", s3.delete_bucket, Bucket=g("build_bucket"))
if g("gateway_id"):
    if g("gateway_target_id"):
        tryit("gateway target", ac.delete_gateway_target, gatewayIdentifier=g("gateway_id"), targetId=g("gateway_target_id"))
        gone(ac.get_gateway_target, gatewayIdentifier=g("gateway_id"), targetId=g("gateway_target_id"))
    tryit("gateway", ac.delete_gateway, gatewayIdentifier=g("gateway_id"))
    gone(ac.get_gateway, gatewayIdentifier=g("gateway_id"))
if g("policy_engine_id"):
    if g("policy_id"):
        tryit("policy", ac.delete_policy, policyEngineId=g("policy_engine_id"), policyId=g("policy_id"))
        gone(ac.get_policy, policyEngineId=g("policy_engine_id"), policyId=g("policy_id"))
    tryit("policy engine", ac.delete_policy_engine, policyEngineId=g("policy_engine_id"))
if g("credential_provider_arn"):
    tryit("api key credential provider", ac.delete_api_key_credential_provider, name=f"{PREFIX}-repo-token")
if g("api_id"):
    tryit("http api", apigw.delete_api, ApiId=g("api_id"))
if g("repo_api_function"):
    tryit("lambda", lam.delete_function, FunctionName=g("repo_api_function"))
if g("access_point"):
    tryit("s3files access point", s3files.delete_access_point, accessPointId=g("access_point"))
for m in g("mount_targets") or []:
    tryit("mount target " + m, s3files.delete_mount_target, mountTargetId=m)
for m in g("mount_targets") or []:
    gone(s3files.get_mount_target, mountTargetId=m)
if g("file_system"):
    tryit("s3files file system", s3files.delete_file_system, fileSystemId=g("file_system"))
    gone(s3files.get_file_system, fileSystemId=g("file_system"))
if g("bucket"):
    b = se.resource("s3").Bucket(g("bucket"))
    try:
        b.object_versions.delete()
    except ClientError:
        pass
    tryit("bucket", s3.delete_bucket, Bucket=g("bucket"))
for r in ("runtime", "gateway", "repo-api", "s3files", "codebuild"):
    name = f"{PREFIX}-{r}"
    try:
        for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    except ClientError:
        pass
    tryit("role " + name, iam.delete_role, RoleName=name)
for prefix in (f"/aws/codebuild/{PREFIX}-image", f"/aws/lambda/{PREFIX}-repo-api",
               f"/aws/bedrock-agentcore/runtimes/{g('runtime_id')}" if g("runtime_id") else None):
    if prefix:
        for lg in logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", []):
            tryit("log group " + lg["logGroupName"], logs.delete_log_group, logGroupName=lg["logGroupName"])
# network last: AgentCore / S3 Files ENIs can take a few minutes to go away
if g("nat"):
    tryit("nat gateway", ec2.delete_nat_gateway, NatGatewayId=g("nat"))
    ec2.get_waiter("nat_gateway_deleted").wait(NatGatewayIds=[g("nat")])
if g("eip"):
    tryit("eip", ec2.release_address, AllocationId=g("eip"))
vpc_gone = True
if g("vpc"):
    # AgentCore and S3 Files release their network interfaces some time after the
    # resources are deleted. Nothing left in the VPC is billed (the NAT gateway is gone).
    for i in range(40):
        enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [g("vpc")]}])["NetworkInterfaces"]
        if not enis:
            break
        if i % 4 == 0:
            print(f"waiting for {len(enis)} service network interface(s) to be released ...")
        time.sleep(30)
    for sg in (g("sg_mount"), g("sg_runtime")):
        if sg:
            try:
                ec2.revoke_security_group_ingress(GroupId=sg, IpPermissions=ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"])
            except (ClientError, IndexError):
                pass
    for sg in (g("sg_mount"), g("sg_runtime")):
        if sg:
            tryit("sg " + sg, ec2.delete_security_group, GroupId=sg)
    for s in (g("private_subnets") or []) + [g("public_subnet")]:
        if s:
            tryit("subnet " + s, ec2.delete_subnet, SubnetId=s)
    for rt in (g("rt_private"), g("rt_public")):
        if rt:
            tryit("route table " + rt, ec2.delete_route_table, RouteTableId=rt)
    if g("igw"):
        tryit("igw detach", ec2.detach_internet_gateway, InternetGatewayId=g("igw"), VpcId=g("vpc"))
        tryit("igw", ec2.delete_internet_gateway, InternetGatewayId=g("igw"))
    vpc_gone = tryit("vpc", ec2.delete_vpc, VpcId=g("vpc"))
if vpc_gone:
    os.rename(STATE_FILE, STATE_FILE + ".deleted")
    print("done")
else:
    print("\nEverything billable is deleted. The VPC is still waiting for service network interfaces;"
          "\nrun ./cleanup.sh again later (.state.json is kept so it knows what is left).")
    sys.exit(2)
