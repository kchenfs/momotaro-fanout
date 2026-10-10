
# Welcome to your CDK Python project!

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
