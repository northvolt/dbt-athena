{% macro athena__drop_relation(relation) -%}
  {%- do adapter.clean_up_table(relation.schema, relation.table) -%}
  {%- do adapter.delete_table(relation) -%}
{% endmacro %}
