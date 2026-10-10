#!/usr/bin/env python3
import aws_cdk as cdk

from momotaro_fanout.momotaro_fanout_stack import MomotaroFanoutStack


app = cdk.App()

# The live restaurant backend.
MomotaroFanoutStack(app, "MomotaroFanoutStack")

# Throwaway copy that the end-to-end tests place orders against before each
# prod deploy. Stripe test mode, its own orders table, and nothing prints.
MomotaroFanoutStack(app, "MomotaroFanoutStaging", stage="staging")

app.synth()
