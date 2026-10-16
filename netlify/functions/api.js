exports.handler = async (event, context) => {
  return {
    statusCode: 200,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*"
    },
    body: JSON.stringify({
      status: "ok",
      message: "UPI Gateway API Working!",
      time: new Date().toISOString()
    })
  };
};
