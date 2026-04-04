import aws_cdk as core
import aws_cdk.assertions as assertions

from momotaro_fanout.momotaro_fanout_stack import MomotaroFanoutStack

# example tests. To run these tests, uncomment this file along with the example
# resource in momotaro_fanout/momotaro_fanout_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = MomotaroFanoutStack(app, "momotaro-fanout")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
