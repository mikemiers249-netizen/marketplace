def post_worker_init(worker):
    from app.email_worker import start_worker
    start_worker(worker.wsgi)


# Avoid storing confirmation/reset secrets in HTTP access logs.
access_log_format = '%(h)s %(t)s %(m)s %(s)s %(b)s %(L)s'
