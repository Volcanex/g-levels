analytics = request.args.get('analytics')
token = request.query_params.get('token')
@app.get('/server-only')
def page():
    pass
@router.get('/dynamic/{id}')
def dynamic():
    pass
