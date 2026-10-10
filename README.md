# Momotaro order backend (momotaro-fanout)

## Staging and end-to-end order tests

Every push to `main` goes through three steps in `.github/workflows/deploy.yml`:

1. Deploy `MomotaroFanoutStaging`, a copy of the backend built from the same
   code (`app.py`, `stage="staging"`).
2. Run `tests/e2e`. It places a takeout order (paid with Stripe's `pm_card_visa`
   test card) and a dine-in order against staging, then waits until each is
   saved and its kitchen ticket is published. The test deletes its own rows.
3. Deploy `MomotaroFanoutStack` (prod), but only if step 2 passed.

Staging cannot reach the restaurant or customers:

- It has its own orders table. Prod's `momotaroOrdersDatabase` is never touched.
  It reads the real menu table so prices match.
- Stripe runs in test mode. Keys come from `/momotaro/staging/*` in SSM, and
  the e2e test refuses to run if the key isn't a test key.
- Kitchen tickets go to `staging/printers/orders/print`, which no printer or
  kitchen display subscribes to. A staging-only IoT rule copies each ticket
  into the staging table as `KITCHEN#<order id>` so the test can check it.
- No receipt email, SMS or contact opt-in API, and no Telegram alerting.

### One-time setup

1. In the Stripe dashboard, switch to **test mode** and copy the secret key
   (`sk_test_...`). Store it in SSM:
   `aws ssm put-parameter --region ca-central-1 --name /momotaro/staging/STRIPE_SECRET_KEY --type SecureString --value sk_test_...`
2. That's it for the backend. On its first run the e2e test registers the
   Stripe test-mode webhook for the staging URL and stores its signing secret in
   `/momotaro/staging/STRIPE_WEBHOOK_SECRET`.

To run the tests by hand against a deployed staging stack:
`E2E_STACK_NAME=MomotaroFanoutStaging pytest -v tests/e2e` (with AWS credentials
that can assume `momotaro-e2e-staging`, or broader).

---


## CDK project notes

This is a blank project for CDK development with Python.

The `cdk.json` file tells the CDK Toolkit how to execute your app.

This project is set up like a standard Python project.  The initialization
process also creates a virtualenv within this project, stored under the `.venv`
directory.  To create the virtualenv it assumes that there is a `python3`
(or `python` for Windows) executable in your path with access to the `venv`
package. If for any reason the automatic creation of the virtualenv fails,
you can create the virtualenv manually.

To manually create a virtualenv on MacOS and Linux:

```
$ python -m venv .venv
```

After the init process completes and the virtualenv is created, you can use the following
step to activate your virtualenv.

```
$ source .venv/bin/activate
```

If you are a Windows platform, you would activate the virtualenv like this:

```
% .venv\Scripts\activate.bat
```

Once the virtualenv is activated, you can install the required dependencies.

```
$ pip install -r requirements.txt
```

At this point you can now synthesize the CloudFormation template for this code.

```
$ cdk synth
```

To add additional dependencies, for example other CDK libraries, just add
them to your `requirements.txt` file and rerun the `python -m pip install -r requirements.txt`
command.

## Useful commands

 * `cdk ls`          list all stacks in the app
 * `cdk synth`       emits the synthesized CloudFormation template
 * `cdk deploy`      deploy this stack to your default AWS account/region
 * `cdk diff`        compare deployed stack with current state
 * `cdk docs`        open CDK documentation

Enjoy!

## Deploying

Pushes to `main` deploy automatically via `.github/workflows/deploy.yml`:
tests run first, then `cdk deploy` runs with keyless AWS access (GitHub OIDC
→ the `momotaro-fanout-github-deploy` role). Pull requests run the tests only.

- The stack's `.env` lives in SSM as the SecureString
  `/momotaro/prod/fanout/DOTENV`. Change config there, not in the workflow.
  Stripe secrets are separate SSM params the Lambdas read at runtime.
- The Stripe layer is built in CI from `momotaro_layer/requirements.txt`
  (Linux wheels). Bump versions there.
- To redeploy without a code change, run the workflow from the Actions tab
  (`workflow_dispatch`).
