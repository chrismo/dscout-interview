"""Candidate-editable pipeline definition."""

from lib.workflow import Build, Call, Deploy, Pipeline

pipeline = Pipeline(
    pool="pipeline",
    tasks=[
        Build("zorch"),
        Build("greeb"),
        Build("blerg", needs=["greeb"]),
        Call("deploy-zorch", needs=["zorch"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-zorch", "zorch")],
        )),
        Call("deploy-greeb", needs=["greeb"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-greeb", "greeb")],
        )),
        Call("deploy-blerg", needs=["blerg", "deploy-greeb"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-blerg", "blerg")],
        )),
    ],
)
