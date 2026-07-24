# Disclaimer — Not Production-Ready

**This is a personal proof-of-concept, not an official AWS product or service,
and it is NOT production-ready.** It is shared for educational and evaluation
purposes only.

- This is **NOT** an official Amazon Web Services (AWS) product or service.
- This is **NOT** production-ready software.
- This code is provided "as-is" without warranty of any kind.
- The author(s) and Amazon Web Services are **NOT** liable for any issues,
  damages, or costs arising from the use of this code.
- You are solely responsible for testing, securing, and validating this code
  before using it in any environment.

## Security

This project does not implement authentication for the chat frontend (the
bundled Open WebUI runs with `WEBUI_AUTH=false` for local demo use). Before
exposing any endpoint beyond your machine, put an authentication and
authorization layer in front of it. Common patterns:

- **Amazon Cognito** user pools fronting the Open WebUI / proxy.
- **Amazon API Gateway** with a Lambda authorizer or IAM authorization in front
  of the proxy endpoint.
- **Application Load Balancer with OIDC** (e.g., Okta, Microsoft Entra ID) for
  browser access.

The AgentCore Gateway is already protected by Cognito machine-to-machine
(OAuth client-credentials) as deployed. For guidance, see
[Controlling access to APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-control-access-to-api.html)
and [Amazon Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html).

## Cost

Deploying this project will create AWS resources that incur costs. You are
responsible for any charges. Be sure to tear down resources when not in use.

## Data

This project processes scientific data (e.g., .h5ad single-cell datasets).
Ensure you have appropriate permissions and comply with all applicable data
governance policies before processing any data through this system.
