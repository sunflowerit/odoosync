.. image:: https://img.shields.io/badge/licence-AGPL--3-blue.svg
    :alt: License: AGPL-3

odoosync
========

This Python package allows to sync between one Odoo and another.

Installation
============

With pip
--------

To install or upgrade:

    pip install --user https://github.com/sunflowerit/odoosync/archive/master.zip

To add the script to the path, add the following to your `$HOME/.profile`:

    if [ -d "$HOME/.local/bin" ] ; then
        PATH="$HOME/.local/bin:$PATH"
    fi

Using the Anybox buildout recipe
--------------------------------

Add the following to `buildout.cfg`:

    [buildout]
    extensions = gp.vcsdevelop
    vcs-extend-develop = git+ssh://git@github.com/sunflowerit/odoosync.git@master#egg=odoosync-0.1

    [odoo]
    odoo_scripts =
        odoosync=odoosync

Configuration
=============

To specify login and password, put this in `$HOME/.netrc`:

    machine source.domain.tld login me@sunflowerweb.nl password mypassword
    machine destination.domain.tld login me@sunflowerweb.nl password mypassword

To specify which models to sync, create a YAML file.
For examples, please see the `examples` folder in this repository.

Usage
=====

From command line:

    odoosync mysyncfile.yaml

From other Python scripts:

    from odoosync.ModelSyncer import ModelSyncer

Credits
=======

Contributors
------------    

* Hayyan Ebrahem <hayyan-ebrahem@hotmail.com>
* Tom Blauwendraat <tom@sunflowerweb.nl>

Maintainer
----------

.. image:: https://odoo-community.org/logo.png
   :alt: Odoo Community Association
   :target: https://odoo-community.org

This module is maintained by Sunflower IT.


To add to `buildout.cfg`:

    [buildout]
    extensions = gp.vcsdevelop
    vcs-extend-develop = git+ssh://git@github.com/sunflowerit/odoosync.git@master#egg=odoosync-0.1

    [odoo]
    odoo_scripts = 
        odoosync=odoosync


