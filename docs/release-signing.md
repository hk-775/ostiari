# Official release image signing

Ostiari signs official ECR images by immutable digest with keyless Sigstore
certificates. GitHub Actions supplies the OIDC identity, AWS STS grants a
short-lived ECR session, Fulcio issues the signing certificate, and Rekor
records the transparency-log entry. No private signing key or long-lived AWS
credential is stored in GitHub.

## Trust boundary

- Workflow: `.github/workflows/sign-official-images.yml`
- GitHub environment: `production-signing`
- Allowed deployment branch: `main`
- OIDC audience: `sts.amazonaws.com`
- OIDC subject:
  `repo:hk-775/ostiari:environment:production-signing`
- ECR scope: `ostiari/control-plane`, `ostiari/gateway`,
  `ostiari/frontend`, and `ostiari/agentcore`
- Certificate identity:
  `https://github.com/hk-775/ostiari/.github/workflows/sign-official-images.yml@refs/heads/main`
- Certificate issuer: `https://token.actions.githubusercontent.com`

The AWS role may authenticate to ECR and read or write image/signature content
only in those four repositories. It cannot build, deploy, mutate IAM, or access
application secrets.

## One-time AWS bootstrap

An IAM OIDC provider for `token.actions.githubusercontent.com` with audience
`sts.amazonaws.com` must already exist. Deploy the scoped role:

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name OstiariReleaseSigning \
  --template-file deploy/aws/release-signing-role.json \
  --parameter-overrides \
    GitHubOidcProviderArn=arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com \
  --capabilities CAPABILITY_IAM \
  --no-cli-pager
```

The template and its project-specific compliance rules can be checked with:

```bash
cfn-lint --format json --regions us-east-1 \
  --template deploy/aws/release-signing-role.json

cfn-guard validate \
  --rules deploy/aws/release-signing.guard \
  --data deploy/aws/release-signing-role.json \
  --type CFNTemplate \
  --output-format json
```

## GitHub environments

Create `production-signing`, `production-evidence`, and `pypi`; restrict all
three to `main`, disable administrator bypass where the plan supports it, and
require reviewer approval before a job may start. Required-reviewer environment
rules may be unavailable for private repositories on some GitHub billing tiers;
do not perform a public signing, evidence-retention, or package-publication run
until the reviewer rule is active.

Set these non-secret variables on `production-signing`:

| Variable | Value |
|---|---|
| `AWS_SIGNING_ROLE_ARN` | `RoleArn` output from `OstiariReleaseSigning` |
| `AWS_ACCOUNT_ID` | Account containing the official ECR repositories |
| `AWS_REGION` | Region containing the official ECR repositories |
| `ECR_REPOSITORY_PREFIX` | `ostiari` |

Do not add AWS access keys, Cosign private keys, or signing passwords.

## PyPI trusted publishing

Before publishing the first GitHub release, create pending Trusted Publishers
for all four distributions:

| PyPI project | GitHub owner | Repository | Workflow | Environment |
|---|---|---|---|---|
| `ostiari` | `hk-775` | `ostiari` | `publish.yml` | `pypi` |
| `ostiari-gateway` | `hk-775` | `ostiari` | `publish.yml` | `pypi` |
| `ostiari-control-plane` | `hk-775` | `ostiari` | `publish.yml` | `pypi` |
| `axon-llm` | `hk-775` | `ostiari` | `publish.yml` | `pypi` |

The release workflow publishes the reviewed bundled AxonLLM distribution
before the three first-party distributions, then attaches all four
distributions and the SBOM to the GitHub release. Do not add a PyPI API token.

## Sign a release

1. Create the immutable release tag and a draft GitHub release for the exact
   commit. The tag is `v` followed by the canonical PEP 440 package version;
   for this beta use `v0.3.0b2`, not `v0.3.0-beta.2`.
2. Publish the images with `./deploy/ostiari aws publish-images`; retain the
   resulting digest references.
3. Run `Sign official Ostiari images` from `main`, supplying the exact release
   commit, release tag, and image digests.
4. Confirm the run verifies every ECR tag/digest pair, signs each digest,
   verifies the expected certificate identity and Rekor entry, and uploads:
   - `*.sigstore.json` bundles;
   - `*.verification.json` verification results;
   - `SHA256SUMS`;
   - `manifest.json`.
5. Retain the workflow artifact and the copies attached to the immutable GitHub
   release.

The workflow refuses a tag that differs from the canonical package version,
non-SHA release commits, malformed digests, ECR digests that do not match the
release commit tag, missing releases, unexpected workflow refs, and missing
signer configuration.

## Verify as a consumer

After authenticating to the official registry, verify an image by digest:

```bash
cosign verify \
  --certificate-identity \
    https://github.com/hk-775/ostiari/.github/workflows/sign-official-images.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/ostiari/gateway@sha256:DIGEST
```

Verification checks the signed digest, Fulcio certificate, expected GitHub
workflow identity, and Rekor transparency-log proof.
