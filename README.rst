.. image:: https://img.shields.io/badge/licence-AGPL--3-blue.svg
    :alt: License: AGPL-3

odoosync
========

This Python package allows to sync between one Odoo and another.

Main features:

* Sync between different versions of Odoo
* Specify which models to sync
* Specify which records to sync, by specifying a domain
* Initial sync of all specified models/records
* Subsequent syncs only sync changed records
* Follow many2one relations to sync dependent records
* Manual mapping table (id - id) for records that cannot be synced
  (eg. for company ids, country records, analytic account ids...)
* Exclude certain fields from sync
* Include only certain fields in sync
* Bidirectional sync
* 'Dry run' mode

Installation
============

With pip
--------

To install or upgrade::

    pip install --user https://github.com/sunflowerit/odoosync/archive/master.zip

To add the script to the path, add the following to your `$HOME/.profile`::

    if [ -d "$HOME/.local/bin" ] ; then
        PATH="$HOME/.local/bin:$PATH"
    fi

Using the Anybox buildout recipe
--------------------------------

Add the following to `buildout.cfg`::

    [buildout]
    extensions = gp.vcsdevelop
    vcs-extend-develop = git+ssh://git@github.com/sunflowerit/odoosync.git@master#egg=odoosync-0.1

    [odoo]
    odoo_scripts =
        odoosync=odoosync

Configuration
=============

To specify login and password, put this in `$HOME/.netrc`::

    machine source.domain.tld login me@sunflowerweb.nl password mypassword
    machine destination.domain.tld login me@sunflowerweb.nl password mypassword

To specify which models to sync, create a YAML file.
Please see the `YAML examples <https://github.com/sunflowerit/odoosync/blob/master/examples>`_.

Usage
=====

From command line::

    odoosync mysyncfile.yaml

From other Python scripts::

    from odoosync.ModelSyncer import ModelSyncer

Known issues / Roadmap
======================

* Initial sync not tested for bidirectional configuration. Probably it will go well, otherwise sync one way first and later add the reverse sync rules.
* Also sync deletions (or make the record inactive)
* Allow defining python functions in the YAML in order to do conversion of field values
* Allow defining model mapping in the YAML in order to sync from one model to another model (eg. issues to tasks)
* In order to speed up initial sync, use Odoo's import_data or load functionality over RPC to bulk create records, instead of importing one by one
* Investigate the use of Odoo's import functionality to resolve many2one relationships by intelligent naming of xmlids
* Should we refactor and store external identifiers on both sides? Will make the code cleaner and probably faster
* Optimization: dont load dependent records that are already in target system. Unless they have to be created, we dont need to know about them or their dependencies.
* Optimization: we now read each destination record before writing, to check for any change; this could be done in bulk at the beginning 
* Make a Javascript/PouchDB version, so that it also can be used in web applications (eg. a cool ETL tool)
* Save loaded records and translation tables in a lock file (or in PouchDB), so that we dont have to reload from server on next sync. This could also be a case for making the syncer into a permanently running daemon
* YAML structure could be prettier, eg.::
      models:
        * normal:
          res.partner:
          - ...
        * dependent: [....]
      reverse_models
        * normal:
          res.partner:
          - ...
        * dependent: [....]
* Correctly solve sync conflicts by carefully looking at the timestamp, taking into account the clock difference between the two odoo instances. (current behaviour: normal sync 'wins' because it is first
* When syncing mails, use a context of mail_create_nosubscribe = True

Credits
=======

Contributors
------------    

* Hayyan Ebrahem
* Tom Blauwendraat

Maintainer
----------

This module is maintained by Sunflower IT.

