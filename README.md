To install or upgrade:

    pip install --user https://github.com/sunflowerit/odoosync/archive/master.zip

Add to your `$HOME/.profile`:

    if [ -d "$HOME/.local/bin" ] ; then
        PATH="$HOME/.local/bin:$PATH"
    fi

To specify login and password, put this in `$HOME/.netrc`:

    machine machinename login me@sunflowerweb.nl password mypassword    

To use the syncer from the command line:

    odoosync mysyncfile.yaml

To import into your own Python scripts:

    from odoosync.ModelSyncer import ModelSyncer

To add to `buildout.cfg`:

    extensions = gp.vcsdevelop
    vcs-extend-develop = git+ssh://git@github.com/sunflowerit/odoosync.git@master#egg=odoosync-0.1

