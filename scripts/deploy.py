"""Deploy the sample: Codex CLI on Amazon Bedrock AgentCore Runtime.

Creates (all names start with PREFIX):
  VPC (2 private subnets in AgentCore-supported AZs, 1 NAT gateway)
  Amazon S3 Files file system + access point            -> /mnt/archive
  Mock repo API (AWS Lambda + Amazon API Gateway HTTP API) needing a bearer token
  AgentCore Identity API key credential provider          (holds that token)
  AgentCore Gateway (IAM inbound auth) + OpenAPI target  -> MCP tools for Codex
  AgentCore Policy engine (Cedar, ENFORCE)               -> allow list/create PR only
  Container image built by AWS CodeBuild (arm64)        -> Amazon ECR (no local Docker needed)
  Execution role + AgentCore Runtime                     (session storage /mnt/workspace)

State is kept in .state.json so every step can be re-run. Re-running after a change
under agent/ builds a new image and updates the Runtime. cleanup.py deletes everything.
Usage: ./deploy.sh            (region from AWS_REGION, default us-east-1)
"""
import argparse, hashlib, io, json, os, secrets, sys, time, zipfile
import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
STATE_FILE = os.path.join(HERE, ".state.json")
PREFIX = "codex-demo"
MODEL = os.environ.get("CODEX_MODEL", "global.openai.gpt-6.1-sol")
CODEX_VERSION = "0.166.0"
# AgentCore Runtime VPC support is limited to some AZ IDs; see the AgentCore VPC docs.
SUPPORTED_AZ_IDS = {"us-east-1": ["use1-az1", "use1-az2", "use1-az4"],
                    "us-west-2": ["usw2-az1", "usw2-az2", "usw2-az3"]}

p = argparse.ArgumentParser()
p.add_argument("--region", help="default: region of an existing .state.json, else AWS_REGION, else us-east-1")
args = p.parse_args()
_prev = json.load(open(STATE_FILE)).get("region") if os.path.exists(STATE_FILE) else None
REGION = args.region or _prev or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
if REGION not in SUPPORTED_AZ_IDS:
    sys.exit(f"Region {REGION} is not set up in this demo. Use one of: {', '.join(SUPPORTED_AZ_IDS)}")
session = boto3.Session(region_name=REGION)
ACCOUNT = session.client("sts").get_caller_identity()["Account"]
ec2, iam, s3 = session.client("ec2"), session.client("iam"), session.client("s3")
s3files, lam, apigw = session.client("s3files"), session.client("lambda"), session.client("apigatewayv2")
ac, ecr = session.client("bedrock-agentcore-control"), session.client("ecr")
cb, logs = session.client("codebuild"), session.client("logs")

state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
if state.get("region", REGION) != REGION:
    sys.exit(f".state.json belongs to {state['region']}; run cleanup there first or set AWS_REGION={state['region']}")
state["region"] = REGION


def save():
    json.dump(state, open(STATE_FILE, "w"), indent=2, default=str)


def step(name):
    def wrap(fn):
        def run():
            if name in state.get("done", []):
                print(f"= {name} (done)")
                return
            print(f"> {name}")
            fn()
            state.setdefault("done", []).append(name)
            save()
        return run
    return wrap


def wait(desc, check, timeout=900, every=10):
    t0 = time.time()
    while True:
        status = check()
        if status is True:
            return
        if time.time() - t0 > timeout:
            sys.exit(f"timeout waiting for {desc}: {status}")
        time.sleep(every)


def tags(name):
    return [{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": name}]}]


def role(name, service, policy, managed=()):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
             "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    if policy:
        iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": policy}))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


# ---------------------------------------------------------------- network
@step("vpc")
def vpc():
    azs = {z["ZoneId"]: z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"]}
    az_ids = [a for a in SUPPORTED_AZ_IDS[REGION] if a in azs][:2]
    v = ec2.create_vpc(CidrBlock="10.80.0.0/16", TagSpecifications=tags(PREFIX))["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=v, EnableDnsHostnames={"Value": True})
    pub = ec2.create_subnet(VpcId=v, CidrBlock="10.80.0.0/24", AvailabilityZoneId=az_ids[0])["Subnet"]["SubnetId"]
    priv = [ec2.create_subnet(VpcId=v, CidrBlock=f"10.80.{i + 1}.0/24", AvailabilityZoneId=a)["Subnet"]["SubnetId"]
            for i, a in enumerate(az_ids)]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=v)
    rt_pub = ec2.create_route_table(VpcId=v)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rt_pub, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    ec2.associate_route_table(RouteTableId=rt_pub, SubnetId=pub)
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(SubnetId=pub, AllocationId=eip)["NatGateway"]["NatGatewayId"]
    state.update(vpc=v, public_subnet=pub, private_subnets=priv, igw=igw, rt_public=rt_pub, eip=eip, nat=nat)
    save()
    ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat])
    rt_priv = ec2.create_route_table(VpcId=v)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rt_priv, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
    for s in priv:
        ec2.associate_route_table(RouteTableId=rt_priv, SubnetId=s)
    sg_rt = ec2.create_security_group(GroupName=f"{PREFIX}-runtime", Description="AgentCore Runtime ENIs", VpcId=v)["GroupId"]
    sg_mt = ec2.create_security_group(GroupName=f"{PREFIX}-s3files-mt", Description="S3 Files mount targets", VpcId=v)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg_mt, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
                                         "UserIdGroupPairs": [{"GroupId": sg_rt}]}])
    state.update(rt_private=rt_priv, sg_runtime=sg_rt, sg_mount=sg_mt)


# ---------------------------------------------------------------- S3 Files
@step("s3files")
def s3_files():
    bucket = f"{PREFIX}-archive-{ACCOUNT}-{REGION}"
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})   # required by S3 Files
    state["bucket"] = bucket
    save()
    cond = {"StringEquals": {"aws:ResourceAccount": ACCOUNT}}
    fs_role = role(f"{PREFIX}-s3files", "elasticfilesystem.amazonaws.com", [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:ListBucketVersions"], "Resource": f"arn:aws:s3:::{bucket}", "Condition": cond},
        {"Effect": "Allow", "Action": ["s3:AbortMultipartUpload", "s3:DeleteObject*", "s3:GetObject*", "s3:List*", "s3:PutObject*"],
         "Resource": f"arn:aws:s3:::{bucket}/*", "Condition": cond},
        {"Effect": "Allow", "Action": ["events:DeleteRule", "events:DisableRule", "events:EnableRule", "events:PutRule",
                                       "events:PutTargets", "events:RemoveTargets"],
         "Condition": {"StringEquals": {"events:ManagedBy": "elasticfilesystem.amazonaws.com"}},
         "Resource": "arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*"},
        {"Effect": "Allow", "Action": ["events:DescribeRule", "events:ListRuleNamesByTarget", "events:ListRules",
                                       "events:ListTargetsByRule"], "Resource": "arn:aws:events:*:*:rule/*"}])
    time.sleep(10)   # IAM propagation
    fs = s3files.create_file_system(bucket=f"arn:aws:s3:::{bucket}", roleArn=fs_role)
    state.update(s3files_role=fs_role, file_system=fs["fileSystemId"], file_system_arn=fs["fileSystemArn"])
    save()
    wait("file system", lambda: s3files.get_file_system(fileSystemId=fs["fileSystemId"])["status"] == "available" or
         s3files.get_file_system(fileSystemId=fs["fileSystemId"])["status"])
    mts = [s3files.create_mount_target(fileSystemId=fs["fileSystemId"], subnetId=s,
                                       securityGroups=[state["sg_mount"]])["mountTargetId"] for s in state["private_subnets"]]
    state["mount_targets"] = mts
    save()
    wait("mount targets", lambda: all(s3files.get_mount_target(mountTargetId=m)["status"] == "available" for m in mts) or "creating")
    ap = s3files.create_access_point(fileSystemId=fs["fileSystemId"], posixUser={"uid": 1000, "gid": 1000},
                                     rootDirectory={"path": "/codex", "creationPermissions":
                                                    {"ownerUid": 1000, "ownerGid": 1000, "permissions": "750"}})
    state.update(access_point=ap["accessPointId"], access_point_arn=ap["accessPointArn"])
    save()
    wait("access point", lambda: s3files.get_access_point(accessPointId=ap["accessPointId"])["status"] == "available" or "creating")


# ---------------------------------------------------------------- mock repo API
@step("repo_api")
def repo_api():
    token = "repo_" + secrets.token_urlsafe(24)       # stands in for a GitHub token
    fn_role = role(f"{PREFIX}-repo-api", "lambda.amazonaws.com", None,
                   ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    time.sleep(10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(os.path.join(HERE, "repo_api", "handler.py"), "handler.py")
    fn = lam.create_function(FunctionName=f"{PREFIX}-repo-api", Runtime="python3.13", Architectures=["arm64"],
                             Role=fn_role, Handler="handler.lambda_handler", Code={"ZipFile": buf.getvalue()},
                             Environment={"Variables": {"REPO_API_TOKEN": token}}, Timeout=10)
    lam.get_waiter("function_active_v2").wait(FunctionName=fn["FunctionName"])
    api = apigw.create_api(Name=f"{PREFIX}-repo-api", ProtocolType="HTTP", Target=fn["FunctionArn"])
    lam.add_permission(FunctionName=fn["FunctionName"], StatementId="apigw", Action="lambda:InvokeFunction",
                       Principal="apigateway.amazonaws.com",
                       SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api['ApiId']}/*")
    state.update(repo_api_role=fn_role, repo_api_function=fn["FunctionName"], api_id=api["ApiId"],
                 api_url=api["ApiEndpoint"], repo_token=token)


# ---------------------------------------------------------------- Identity
@step("identity")
def identity():
    r = ac.create_api_key_credential_provider(name=f"{PREFIX}-repo-token", apiKey=state["repo_token"])
    state.update(credential_provider_arn=r["credentialProviderArn"], credential_secret_arn=r["apiKeySecretArn"]["secretArn"])
    # The token now lives only in the Identity token vault; drop it from local state.
    state["repo_token"] = "<stored in AgentCore Identity>"


# ---------------------------------------------------------------- Gateway
@step("gateway")
def gateway():
    gw_name = f"{PREFIX}-gw"
    gw_role = role(f"{PREFIX}-gateway", "bedrock-agentcore.amazonaws.com", [
        {"Effect": "Allow", "Action": "bedrock-agentcore:GetWorkloadAccessToken", "Resource": [
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default",
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default/workload-identity/{gw_name}-*"]},
        {"Effect": "Allow", "Action": "bedrock-agentcore:GetResourceApiKey", "Resource": [
            state["credential_provider_arn"],
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default",
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default",
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default/workload-identity/{gw_name}-*"]},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": state["credential_secret_arn"]}])
    time.sleep(10)
    gw = ac.create_gateway(name=gw_name, roleArn=gw_role, protocolType="MCP", authorizerType="AWS_IAM")
    state.update(gateway_role=gw_role, gateway_id=gw["gatewayId"], gateway_arn=gw["gatewayArn"], gateway_url=gw["gatewayUrl"])
    save()
    wait("gateway", lambda: ac.get_gateway(gatewayIdentifier=gw["gatewayId"])["status"] == "READY" or "creating")
    spec = open(os.path.join(HERE, "repo_api", "openapi.json")).read().replace("${API_URL}", state["api_url"])
    t = ac.create_gateway_target(
        gatewayIdentifier=gw["gatewayId"], name="repo",
        targetConfiguration={"mcp": {"openApiSchema": {"inlinePayload": spec}}},
        credentialProviderConfigurations=[{"credentialProviderType": "API_KEY", "credentialProvider": {
            "apiKeyCredentialProvider": {"providerArn": state["credential_provider_arn"],
                                         "credentialParameterName": "Authorization",
                                         "credentialPrefix": "Bearer", "credentialLocation": "HEADER"}}}])
    state["gateway_target_id"] = t["targetId"]
    save()
    wait("gateway target", lambda: ac.get_gateway_target(gatewayIdentifier=gw["gatewayId"], targetId=t["targetId"])["status"] == "READY"
         or ac.get_gateway_target(gatewayIdentifier=gw["gatewayId"], targetId=t["targetId"])["status"])


# ---------------------------------------------------------------- Policy (Cedar)
@step("policy")
def policy():
    if "policy_engine_id" not in state:
        pe = ac.create_policy_engine(name=PREFIX.replace("-", "_") + "_guard")
        state.update(policy_engine_id=pe["policyEngineId"], policy_engine_arn=pe["policyEngineArn"])
        save()
    pe_id, pe_arn = state["policy_engine_id"], state["policy_engine_arn"]
    wait("policy engine", lambda: ac.get_policy_engine(policyEngineId=pe_id)["status"] == "ACTIVE" or "creating")
    statement = f'''permit (
    principal,
    action in [AgentCore::Action::"repo___listPullRequests",
               AgentCore::Action::"repo___createPullRequest"],
    resource == AgentCore::Gateway::"{state['gateway_arn']}"
);'''
    if "policy_id" not in state:
        pol = ac.create_policy(policyEngineId=pe_id, name="allow_read_and_open_pr",
                               definition={"cedar": {"statement": statement}})
        state["policy_id"] = pol["policyId"]
        save()
    # The gateway role must be able to read the engine and ask it for decisions.
    iam.put_role_policy(RoleName=f"{PREFIX}-gateway", PolicyName="policy-engine", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:GetPolicyEngine", "Resource": pe_arn},
            {"Effect": "Allow", "Action": ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"],
             "Resource": [pe_arn, state["gateway_arn"]]}]}))
    time.sleep(15)
    gw = ac.get_gateway(gatewayIdentifier=state["gateway_id"])
    ac.update_gateway(gatewayIdentifier=state["gateway_id"], name=gw["name"], roleArn=gw["roleArn"],
                      protocolType="MCP", authorizerType="AWS_IAM",
                      policyEngineConfiguration={"arn": pe_arn, "mode": "ENFORCE"})
    wait("gateway update", lambda: ac.get_gateway(gatewayIdentifier=state["gateway_id"])["status"] == "READY" or "updating")


# ---------------------------------------------------------------- image (AWS CodeBuild)
BUILDSPEC = """version: 0.2
env:
  shell: bash
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${IMAGE_URI%%/*}
  build:
    commands:
      - docker build --build-arg CODEX_VERSION=$CODEX_VERSION -t $IMAGE_URI .
      - docker push $IMAGE_URI
"""


def agent_zip():
    """Zip agent/ deterministically; the content hash becomes the image tag."""
    buf, h = io.BytesIO(), hashlib.sha256()
    root = os.path.join(HERE, "agent")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            data = open(path, "rb").read()
            h.update(name.encode() + b"\0" + data)
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            z.writestr(info, data)
    return buf.getvalue(), h.hexdigest()[:12]


@step("build_project")
def build_project():
    repo = f"{PREFIX}-agent"
    try:
        uri = ecr.create_repository(repositoryName=repo, imageScanningConfiguration={"scanOnPush": True},
                                    encryptionConfiguration={"encryptionType": "AES256"})["repository"]["repositoryUri"]
    except ecr.exceptions.RepositoryAlreadyExistsException:
        uri = ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]["repositoryUri"]
    bucket = f"{PREFIX}-build-{ACCOUNT}-{REGION}"
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    state.update(ecr_repo=repo, ecr_uri=uri, build_bucket=bucket)
    save()
    cb_role = role(f"{PREFIX}-codebuild", "codebuild.amazonaws.com", [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/codebuild/{PREFIX}-image*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                                       "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage"],
         "Resource": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{repo}"}])
    time.sleep(10)
    cb.create_project(
        name=f"{PREFIX}-image", serviceRole=cb_role,
        source={"type": "S3", "location": f"{bucket}/agent.zip", "buildspec": BUILDSPEC},
        artifacts={"type": "NO_ARTIFACTS"},
        environment={"type": "ARM_CONTAINER", "image": "aws/codebuild/amazonlinux-aarch64-standard:3.0",
                     "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True},
        logsConfig={"cloudWatchLogs": {"status": "ENABLED", "groupName": f"/aws/codebuild/{PREFIX}-image"}})
    state.update(codebuild_role=cb_role, codebuild_project=f"{PREFIX}-image")


def image():
    """Build only when agent/ changed (content hash = image tag)."""
    data, digest = agent_zip()
    tag = f"{state['ecr_uri']}:{CODEX_VERSION}-{digest}"
    if state.get("image") == tag:
        print(f"= image {tag} (unchanged)")
        return
    print(f"> image: building {tag} with AWS CodeBuild (about 3-5 minutes)")
    s3.put_object(Bucket=state["build_bucket"], Key="agent.zip", Body=data)
    b = cb.start_build(projectName=state["codebuild_project"], environmentVariablesOverride=[
        {"name": "IMAGE_URI", "value": tag}, {"name": "CODEX_VERSION", "value": CODEX_VERSION}])["build"]
    while True:
        b = cb.batch_get_builds(ids=[b["id"]])["builds"][0]
        if b["buildStatus"] != "IN_PROGRESS":
            break
        time.sleep(15)
    if b["buildStatus"] != "SUCCEEDED":
        save()
        sys.exit(f"image build {b['buildStatus']}; logs: {b.get('logs', {}).get('deepLink')}")
    state["image"] = tag
    save()


# ---------------------------------------------------------------- Runtime
@step("runtime")
def runtime():
    name = PREFIX.replace("-", "_")
    exec_role = role(f"{PREFIX}-runtime", "bedrock-agentcore.amazonaws.com", [
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": [
            "arn:aws:bedrock:*::foundation-model/openai.*",
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/*.openai.*"]},
        # The OpenAI-compatible endpoint (/openai/v1/responses) also authorizes against the default Bedrock project.
        {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:project/default"},
        {"Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{state['ecr_repo']}"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                       "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
         "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/*"},
        {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                                       "xray:GetSamplingRules", "xray:GetSamplingTargets"], "Resource": "*"},
        {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": state["gateway_arn"]},
        {"Effect": "Allow", "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
         "Resource": state["file_system_arn"],
         "Condition": {"ArnEquals": {"s3files:AccessPointArn": state["access_point_arn"]}}},
        # Checked by CreateAgentRuntime without the access point condition key.
        {"Effect": "Allow", "Action": ["s3files:GetAccessPoint", "s3files:ListMountTargets"],
         "Resource": [state["file_system_arn"], state["access_point_arn"]]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{state['bucket']}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{state['bucket']}"}])
    state["runtime_role"] = exec_role
    save()
    time.sleep(15)
    rt = ac.create_agent_runtime(
        agentRuntimeName=name, roleArn=exec_role,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": state["image"]}},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
            "subnets": state["private_subnets"], "securityGroups": [state["sg_runtime"]]}},
        filesystemConfigurations=[
            {"sessionStorage": {"mountPath": "/mnt/workspace"}},
            {"s3FilesAccessPoint": {"accessPointArn": state["access_point_arn"], "mountPath": "/mnt/archive"}}],
        environmentVariables={"GATEWAY_URL": state["gateway_url"], "CODEX_MODEL": MODEL, "AWS_REGION": REGION},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800})
    state.update(runtime_id=rt["agentRuntimeId"], runtime_arn=rt["agentRuntimeArn"])
    save()
    wait("runtime", lambda: ac.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])["status"] == "READY"
         or ac.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])["status"])


def sync_runtime():
    """Point the Runtime at the current image (a new version resets session storage)."""
    rt = ac.get_agent_runtime(agentRuntimeId=state["runtime_id"])
    if rt["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == state["image"]:
        return
    print(f"> runtime: updating to {state['image']}")
    net = rt["networkConfiguration"]
    net.get("networkModeConfig", {}).pop("requireServiceS3Endpoint", None)   # read-only after create
    ac.update_agent_runtime(agentRuntimeId=state["runtime_id"], roleArn=rt["roleArn"],
                            agentRuntimeArtifact={"containerConfiguration": {"containerUri": state["image"]}},
                            networkConfiguration=net, filesystemConfigurations=rt["filesystemConfigurations"],
                            environmentVariables=rt.get("environmentVariables") or {},
                            lifecycleConfiguration=rt["lifecycleConfiguration"])
    time.sleep(5)
    wait("runtime update", lambda: ac.get_agent_runtime(agentRuntimeId=state["runtime_id"])["status"] == "READY" or "updating")


if __name__ == "__main__":
    for s in (vpc, s3_files, repo_api, identity, gateway, policy, build_project, image, runtime, sync_runtime):
        try:
            s()
        except ClientError as e:
            save()
            sys.exit(f"failed: {e}")
    print(json.dumps({k: state[k] for k in ("runtime_arn", "gateway_url")}, indent=2))
