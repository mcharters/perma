# -*- coding: utf-8 -*-
# Generated by Django 1.11.27 on 2020-02-04 17:35
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('perma', '0046_delete_cdxline'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicallink',
            name='submitted_url_surt',
            field=models.CharField(blank=True, max_length=2100, null=True),
        ),
        migrations.AddField(
            model_name='link',
            name='submitted_url_surt',
            field=models.CharField(blank=True, max_length=2100, null=True),
        ),
    ]
