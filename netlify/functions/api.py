def handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json","Access-Control-Allow-Origin":"*"},
        "body": '{"status":"ok","message":"UPI Gateway API Working!"}'
    }
