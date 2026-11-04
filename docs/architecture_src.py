import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
plt.rcParams["font.sans-serif"]=["Noto Sans CJK SC","DejaVu Sans"]
fig,ax=plt.subplots(figsize=(14,7.6),dpi=150);ax.set_xlim(0,14);ax.set_ylim(0,7.6);ax.axis("off")
def box(x,y,w,h,t,fc="#ffffff",ec="#232f3e",fs=9,bold=False,ls="-"):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0.02,rounding_size=0.08",fc=fc,ec=ec,lw=1.3,ls=ls))
    ax.text(x+w/2,y+h/2,t,ha="center",va="center",fontsize=fs,weight="bold" if bold else "normal")
def arrow(x1,y1,x2,y2,t="",tx=None,ty=None,c="#232f3e"):
    ax.annotate("",xy=(x2,y2),xytext=(x1,y1),arrowprops=dict(arrowstyle="->",lw=1.3,color=c))
    if t: ax.text(tx if tx is not None else (x1+x2)/2,ty if ty is not None else (y1+y2)/2+0.15,t,ha="center",fontsize=8,color="#333")
box(0.1,3.7,1.8,1.1,"业务系统 /\n平台流水线",fc="#f2f3f3",fs=9.5)
# VPC
ax.add_patch(FancyBboxPatch((2.4,0.3),6.6,6.9,boxstyle="round,pad=0.02",fc="#f7fbf7",ec="#2e7d32",lw=1.3,ls="--"))
ax.text(2.6,6.95,"客户 VPC（私有子网 + NAT）",fontsize=9,color="#2e7d32",weight="bold")
ax.add_patch(FancyBboxPatch((2.7,2.0),6.0,4.6,boxstyle="round,pad=0.02",fc="#fff8ee",ec="#ff9900",lw=1.5))
ax.text(5.7,6.35,"AgentCore Runtime microVM（每个 runtimeSessionId 一台）",ha="center",fontsize=9.5,weight="bold",color="#c45500")
box(2.95,4.7,2.5,1.2,"server.py :8080\n/invocations  /ping\n(HealthyBusy)",fs=8.5)
box(5.95,4.7,2.5,1.2,"Codex CLI 0.166\namazon-bedrock-runtime\nprovider",fs=8.5)
box(5.95,3.2,2.5,1.0,"mcp-proxy-for-aws\n(stdio → SigV4)",fs=8.5)
box(2.95,3.2,2.5,1.0,"CODEX_HOME 本地盘\nSQLite 状态 / 写锁",fs=8.5,fc="#f7f7f7")
box(2.95,2.2,5.5,0.75,"/mnt/workspace 会话存储：项目文件 + rollout（14 天；版本更新会重置）",fs=8.3,fc="#e8f4fd",ec="#1a73e8")
box(2.95,0.6,5.5,0.9,"/mnt/archive  Amazon S3 Files 访问点 → S3 桶\nrollout 归档（按会话分目录），会话存储重置后自动恢复",fs=8.3,fc="#e8f4fd",ec="#1a73e8")
arrow(5.45,5.3,5.95,5.3)
arrow(7.2,4.7,7.2,4.2)
arrow(5.7,2.2,5.7,1.5,"每轮结束归档",tx=6.6,ty=1.75)
arrow(1.9,4.4,2.95,5.2,"InvokeAgentRuntime",tx=1.75,ty=5.35)
arrow(1.9,4.0,2.95,2.6,"InvokeAgentRuntime\nCommand（跑测试）",tx=1.45,ty=2.75,c="#6a1b9a")
# right side
box(10.0,5.6,3.8,1.1,"Amazon Bedrock\nOpenAI GPT（执行角色 SigV4，无密钥）",fc="#f2f3f3",fs=9,bold=True)
arrow(8.45,5.6,10.0,6.05)
ax.add_patch(FancyBboxPatch((9.7,1.4),4.2,3.7,boxstyle="round,pad=0.02",fc="#f3effa",ec="#6a1b9a",lw=1.3))
ax.text(11.8,4.85,"AgentCore Gateway（IAM 入站）",ha="center",fontsize=9.5,weight="bold",color="#6a1b9a")
box(9.95,3.6,3.7,0.95,"Policy（Cedar，ENFORCE）\n默认拒绝；只放行 list / create PR",fs=8.5)
box(9.95,2.45,3.7,0.9,"Identity Token Vault\n注入 Authorization: Bearer …",fs=8.5)
box(9.95,1.6,3.7,0.65,"OpenAPI target “repo”",fs=8.5)
arrow(8.45,3.7,9.95,4.0,"MCP",tx=9.2,ty=3.95)
box(10.4,0.2,3.0,0.9,"代码仓库 API\n（示例中为 Lambda 模拟）",fc="#f2f3f3",fs=8.5)
arrow(11.8,1.6,11.8,1.1)
ax.text(12.2,6.95,"CloudTrail / CloudWatch GenAI Observability",ha="center",fontsize=8,color="#555")
plt.savefig("architecture.png",bbox_inches="tight",facecolor="white")
